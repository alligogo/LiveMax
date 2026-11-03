"""스트리밍 — /live, /live/<id> (DESIGN 8.3 ④)

- 라이브 목록: 상태, 지금 나가는 곡·영상, 가동시간
- 미리보기 = 유튜브 임베드 플레이어 (서버 CPU 0). "지금 나가는 화면"은 현재 클립의 업로드 시
  썸네일로 대신한다. 송출 스트림에서 실시간 캡처하지 않는다 (DESIGN 8.4, 11-9).
- 라이브별 설정 편집 → config/<id>.yaml 저장. 스트림 키는 secrets/<id>.key (600).
  스트림 키는 조회 시 '****1a2b' 로만 보여주고 평문은 절대 표시하지 않는다.

1단계에서는 저장만 하고, 반영은 다음 일일 재시작 때 일어난다 (조작 버튼은 2단계).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request

from .. import catalog, config, db, paths as _paths, secrets
from . import data
from .common import redirect, render

router = APIRouter()


def _key_masked(lv: config.LiveConfig) -> str:
    try:
        return secrets.mask(lv.stream_key_file.read_text(encoding="utf-8").strip())
    except OSError:
        return ""


def _write_yaml_atomic(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(config.dump_live_yaml(d))
    os.chmod(tmp, 0o640)
    os.replace(tmp, p)


@router.get("/live")
def live_list(request: Request):
    rows, errors = data.live_rows(request.state.conn, request.state.server, request.state.paths)
    return render(request, "live_list.html", rows=rows, errors=errors)


def _form_ctx(request: Request, lv: config.LiveConfig | None, raw: dict, is_new: bool):
    paths = request.state.paths
    return {
        "lv": lv, "raw": raw, "is_new": is_new,
        "video_folders": catalog.list_folders(paths, "video"),
        "audio_folders": catalog.list_folders(paths, "audio"),
        "modes": config.AUDIO_MODES,
        "key_masked": _key_masked(lv) if lv else "",
    }


@router.get("/live/new")
def live_new(request: Request):
    lives, _ = config.load_all_lives(server=request.state.server, cross=False)
    used_ports = {lv.harbor_port for lv in lives}
    port = next(p for p in range(8101, 9000) if p not in used_ports)
    raw = {
        "id": "", "name": "", "enabled": False,
        "video": {"bitrate": "5500k"},
        "audio": {"mode": "random_norepeat", "norepeat_count": 30, "crossfade": 0, "harbor_port": port},
        "timezone": "America/Chicago",
        "schedule": [{"from": "00:00", "video": [], "audio": []}],
        "restart_at": "", "stream_key_file": "", "youtube": {"video_id": ""},
    }
    return render(request, "live_edit.html", **_form_ctx(request, None, raw, True))


@router.get("/live/{live_id}")
def live_detail(request: Request, live_id: str):
    server, paths, conn = request.state.server, request.state.paths, request.state.conn
    try:
        lv = config.load_live(live_id, server=server)
    except config.ConfigError as e:
        return redirect("/live", err=str(e))
    rows, _ = data.live_rows(conn, server, paths)
    row = next((r for r in rows if r["id"] == live_id), None)
    restarts = conn.execute("SELECT * FROM restarts WHERE live_id=? ORDER BY ts DESC LIMIT 20", (live_id,)).fetchall()
    events = conn.execute("SELECT * FROM events WHERE live_id=? ORDER BY ts DESC LIMIT 30", (live_id,)).fetchall()
    return render(request, "live_edit.html", **_form_ctx(request, lv, lv.raw, False), row=row,
                  restarts=restarts, events=events)


async def _parse_form(request: Request, base: dict) -> tuple[dict, str]:
    f = await request.form()
    d = dict(base)
    d["id"] = (f.get("id") or base.get("id") or "").strip()
    d["name"] = (f.get("name") or "").strip()
    d["enabled"] = f.get("enabled") == "on"
    d["video"] = {**(base.get("video") or {}), "bitrate": (f.get("bitrate") or "5500k").strip()}
    audio = dict(base.get("audio") or {})
    audio["mode"] = f.get("mode") or "random_norepeat"
    audio["norepeat_count"] = int(f.get("norepeat_count") or 30)
    audio["crossfade"] = 0
    audio["harbor_port"] = int(f.get("harbor_port") or audio.get("harbor_port") or 0)
    d["audio"] = audio
    d["timezone"] = (f.get("timezone") or "").strip()
    d["restart_at"] = (f.get("restart_at") or "").strip()
    sched = []
    i = 0
    while f.get(f"seg-{i}-from") is not None:
        start = (f.get(f"seg-{i}-from") or "").strip()
        if start and f.get(f"seg-{i}-delete") != "on":
            sched.append({"from": start, "video": f.getlist(f"seg-{i}-video"), "audio": f.getlist(f"seg-{i}-audio")})
        i += 1
    d["schedule"] = sched
    d["stream_key_file"] = base.get("stream_key_file") or f"secrets/{d['id']}.key"
    yt = dict(base.get("youtube") or {})
    yt["video_id"] = (f.get("video_id") or "").strip()
    d["youtube"] = yt
    return d, (f.get("stream_key") or "").strip()


def _save(request: Request, d: dict, key: str, is_new: bool):
    server = request.state.server
    cfg_dir = _paths.CONFIG_DIR
    target = cfg_dir / f"{d.get('id', '')}.yaml"
    try:
        lv = config.parse_live(d, target, server)
    except config.ConfigError as e:
        return None, str(e)
    if is_new and target.exists():
        return None, f"{lv.id} 는 이미 있습니다"
    others, _ = config.load_all_lives(server=server, cross=False)
    others = [o for o in others if o.id != lv.id]
    problems = config.cross_check([*others, lv], server)
    if problems:
        return None, " / ".join(problems)
    if is_new and not key:
        return None, "새 라이브는 스트림 키가 필요합니다"
    if key:
        secrets.write_secret(lv.stream_key_file, key)
    _write_yaml_atomic(target, d)
    return lv, None


@router.post("/live/new")
async def live_create(request: Request):
    d, key = await _parse_form(request, {})
    lv, err = _save(request, d, key, True)
    if err:
        return render(request, "live_edit.html", **_form_ctx(request, None, d, True), err=err)
    db.add_event(request.state.conn, "info", "dashboard", "라이브 설정 생성", lv.id)
    return redirect(f"/live/{lv.id}", ok=(
        f"{lv.id} 설정을 저장했습니다. 송출 시작: sudo systemctl enable --now live-audio@{lv.id} live@{lv.id} "
        "(다른 라이브에는 영향 없음)"))


@router.post("/live/{live_id}")
async def live_save(request: Request, live_id: str):
    try:
        cur = config.load_live(live_id, server=request.state.server)
        base = cur.raw
    except config.ConfigError:
        cur, base = None, {}
    d, key = await _parse_form(request, base)
    d["id"] = live_id  # id 변경은 허용하지 않음
    lv, err = _save(request, d, key, False)
    if err:
        return render(request, "live_edit.html", **_form_ctx(request, cur, d, False), err=err)
    db.add_event(request.state.conn, "info", "dashboard", "라이브 설정 변경" + (" (스트림 키 교체)" if key else ""), live_id)
    return redirect(f"/live/{live_id}", ok="저장했습니다. 영상 편성·비트레이트는 다음 일일 재시작 때, 음원 설정은 오디오 재시작 때 반영됩니다")
