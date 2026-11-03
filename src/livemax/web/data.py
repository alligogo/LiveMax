"""대시보드용 데이터 모음 (조회 전용). 서버 CPU 를 쓰는 캡처·디코딩은 하지 않는다."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .. import capacity, catalog, config, sysmetrics
from ..paths import Paths
from ..state import read_json

# 상태 표시 — 색상만으로 전달하지 않도록 아이콘 + 라벨을 함께 둔다 (DESIGN 8.2)
STATES = {
    "ok": {"label": "정상", "icon": "●"},
    "warn": {"label": "경고", "icon": "▲"},
    "down": {"label": "중단", "icon": "■"},
    "restarting": {"label": "재시작중", "icon": "↻"},
    "disabled": {"label": "비활성", "icon": "○"},
}

_disk_cache: dict = {"ts": 0, "data": None}


def classify(lv: config.LiveConfig, st: dict | None, server: dict, recent_restarts: int) -> tuple[str, str]:
    """(상태 키, 설명)"""
    if not lv.enabled:
        return "disabled", "enabled: false"
    if not st:
        return "down", "상태 정보 없음 (서비스 미기동?)"
    s = st.get("state")
    now = time.time()
    if s == "starting":
        return "restarting", "시작 중"
    if s != "running":
        return "down", st.get("message") or s or "중단"
    if not sysmetrics.pid_alive(st.get("ffmpeg_pid")):
        return "down", "ffmpeg 프로세스 없음"
    if now - float(st.get("updated_at") or 0) > float(server["watchdog"]["stall_seconds"]):
        return "warn", "진행 정지"
    if now - float(st.get("ffmpeg_started_at") or now) < 30:
        return "restarting", "방금 시작됨"
    sp = st.get("speed")
    if sp is not None and float(sp) < float(server["watchdog"]["speed_min"]):
        return "warn", f"speed {float(sp):.2f}x"
    if recent_restarts >= 3:
        return "warn", f"최근 1시간 재시작 {recent_restarts}회"
    return "ok", f"speed {float(sp):.2f}x" if sp is not None else "실행중"


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}일 {h}시간"
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분"


def live_rows(conn, server: dict, paths: Paths) -> tuple[list[dict], dict[str, str]]:
    lives, errors = config.load_all_lives(server=server, cross=False)
    now = time.time()
    rows = []
    for lv in lives:
        st = read_json(paths.status_file(lv.id))
        np_ = read_json(paths.nowplaying_file(lv.id)) or {}
        rr_hour = conn.execute("SELECT COUNT(*) FROM restarts WHERE live_id=? AND ts>?", (lv.id, now - 3600)).fetchone()[0]
        rr_total = conn.execute("SELECT COUNT(*) FROM restarts WHERE live_id=?", (lv.id,)).fetchone()[0]
        last_stop = conn.execute(
            "SELECT ts FROM events WHERE live_id=? AND source='runner' AND message LIKE 'ffmpeg 종료%' ORDER BY ts DESC LIMIT 1",
            (lv.id,)).fetchone()
        key, desc = classify(lv, st, server, rr_hour)
        clip = (st or {}).get("current_clip") or {}
        clip_path = Path(clip["path"]) if clip.get("path") else None
        rows.append({
            "id": lv.id, "name": lv.name, "enabled": lv.enabled, "state": key, "state_desc": desc,
            "label": STATES[key]["label"], "icon": STATES[key]["icon"],
            "speed": (st or {}).get("speed"),
            "uptime": fmt_duration(now - st["ffmpeg_started_at"]) if st and st.get("ffmpeg_started_at") and key in ("ok", "warn") else "-",
            "restarts_hour": rr_hour, "restarts_total": rr_total,
            "last_stop": time.strftime("%m-%d %H:%M", time.localtime(last_stop["ts"])) if last_stop else "-",
            "song": (np_.get("title") or Path(np_.get("file", "")).stem) if np_ else "-",
            "artist": np_.get("artist") or "",
            "clip": clip_path.name if clip_path else "-",
            "clip_folder": clip_path.parent.name if clip_path else "",
            "segment": clip.get("segment", ""),
            "bitrate_kbps": lv.bitrate_kbps,
            "video_id": lv.youtube_video_id,
            "timezone": lv.timezone,
            "restart_at": lv.restart_at,
            "cfg": lv,
        })
    return rows, errors


def system_snapshot(paths: Paths) -> dict:
    return read_json(paths.run_dir / "system.json") or {}


def capacity_view(server: dict, rows: list[dict], sysd: dict) -> capacity.Capacity:
    enabled = [r["bitrate_kbps"] for r in rows if r["enabled"]]
    return capacity.compute(
        server["capacity"], enabled, sysd.get("month_tx_bytes"), sysd.get("net_tx_mbps"),
        sysd.get("cpu_pct"), sysd.get("mem_used_mb"), sysd.get("mem_total_mb"),
    )


def alerts(conn, server: dict, rows: list[dict], sysd: dict, errors: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for k, v in errors.items():
        out.append({"level": "error", "ts": None, "live": None, "message": f"설정 오류 {k}: {v.strip()}"})
    pct = sysd.get("disk_pct")
    if pct is not None:
        if pct >= server["cleanup"]["disk_emergency_percent"]:
            out.append({"level": "error", "ts": None, "live": None, "message": f"디스크 {pct:.1f}% — 긴급 정리 대상"})
        elif pct >= server["cleanup"]["disk_warn_percent"]:
            out.append({"level": "warn", "ts": None, "live": None, "message": f"디스크 {pct:.1f}% 사용 중 (80% 초과)"})
    if sysd.get("steal_pct") is not None and sysd["steal_pct"] > 5:
        out.append({"level": "warn", "ts": None, "live": None, "message": f"steal time {sysd['steal_pct']:.1f}% — 공유 vCPU 경합"})
    if sysd and time.time() - float(sysd.get("ts", 0)) > 60:
        out.append({"level": "warn", "ts": None, "live": None, "message": "지표 수집기 응답 없음 (livemax-collector 확인)"})
    for r in rows:
        if r["state"] in ("warn", "down"):
            out.append({"level": "warn" if r["state"] == "warn" else "error", "ts": None, "live": r["id"],
                        "message": f"{r['label']}: {r['state_desc']}"})
    for e in conn.execute("SELECT * FROM events WHERE level IN ('warn','error') AND ts > ? ORDER BY ts DESC LIMIT 30",
                          (time.time() - 86400,)).fetchall():
        out.append({"level": e["level"], "ts": time.strftime("%m-%d %H:%M", time.localtime(e["ts"])),
                    "live": e["live_id"], "message": e["message"]})
    return out


def _dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def disk_breakdown(paths: Paths, ttl: float = 300) -> list[tuple[str, float]]:
    now = time.time()
    if _disk_cache["data"] is not None and now - _disk_cache["ts"] < ttl:
        return _disk_cache["data"]
    items = [
        ("영상 원본", paths.video), ("음원 원본", paths.audio), ("업로드 대기", paths.incoming),
        ("격리(거부)", paths.rejected), ("concat 목록", paths.concat_dir), ("썸네일", paths.thumbs_dir),
        ("로그", paths.log_dir),
    ]
    data = [(name, _dir_size(p) / 1e9 if p.exists() else 0.0) for name, p in items]
    data.append(("지표 DB", (paths.db.stat().st_size / 1e9) if paths.db.exists() else 0.0))
    _disk_cache.update(ts=now, data=data)
    return data


def folder_usage(lives: list[config.LiveConfig], kind: str) -> dict[str, list[str]]:
    """폴더 → 이 폴더를 편성에 쓰는 라이브 id 목록."""
    out: dict[str, list[str]] = {}
    for lv in lives:
        folders = lv.video_folders() if kind == "video" else lv.audio_folders()
        for f in folders:
            out.setdefault(f, []).append(lv.id)
    return out


def media_listing(conn, paths: Paths, kind: str, folder: str) -> list[dict]:
    rows = []
    for p in catalog.list_files(paths, kind, folder):
        r = conn.execute("SELECT * FROM media_files WHERE path=?", (str(p),)).fetchone()
        pending = conn.execute("SELECT 1 FROM pending_deletes WHERE path=?", (str(p),)).fetchone()
        rows.append({
            "name": p.name, "size_mb": p.stat().st_size / 1e6,
            "duration": (r["duration"] if r else None),
            "bitrate": (r["bitrate_kbps"] if r else None),
            "thumb": (r["thumb"] if r else None),
            "status": "삭제 예약" if pending else ("검증 통과" if r else "미등록"),
        })
    return rows
