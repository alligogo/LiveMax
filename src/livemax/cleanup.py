"""자동 정리 + 디스크 위생 — livemaxd 가 매시간 실행 (DESIGN 6.4)

24/7 로 몇 달을 도는 시스템에서 디스크가 서서히 차는 것이 가장 흔한 장애 원인이다.
사람이 기억해서 지우는 구조로 만들지 않는다.

정리 대상 (파생물·임시물만):
  - 썸네일 캐시: 원본이 사라진 고아 썸네일
  - concat 목록: 현재 목록 + 오늘(UTC) 것만 유지
  - 업로드 임시 파일: .tmp/*.part 와 _incoming 에 멈춘 전송 — 24시간 후
  - _rejected/ 격리 파일: 7일 후 (사유는 DB rejections 에 남아 있음)
  - 로그: logrotate 가 돌린 압축본 중 보관 기간 초과분
  - 지표 DB: 원본 30일, 그 이전은 시간 단위로 집계 후 축약
  - 런타임 찌꺼기: 없어진 라이브의 /run/livemax 파일

절대 건드리지 않는 것: /media/video/, /media/audio/ 의 업로드 원본 (DESIGN 11-11).
  → safe_unlink() 가 보호 경로 아래의 삭제를 무조건 거부한다. 원본 삭제는 사용자가 웹에서 직접 할 때뿐이다
    (라이브가 쓰는 폴더의 파일은 웹에서도 지울 수 없다).

systemd 에서 IOSchedulingClass=idle (= ionice -c3), Nice=19 로 실행되어 송출을 방해하지 않는다.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import LiveStore
from .paths import Paths, is_within

log = logging.getLogger("livemax.cleanup")


class ProtectedPathError(RuntimeError):
    pass


@dataclass
class Report:
    removed: dict[str, int] = field(default_factory=dict)
    bytes: dict[str, int] = field(default_factory=dict)

    def add(self, cat: str, size: int) -> None:
        self.removed[cat] = self.removed.get(cat, 0) + 1
        self.bytes[cat] = self.bytes.get(cat, 0) + size

    def summary(self) -> str:
        if not self.removed:
            return "정리할 항목 없음"
        return ", ".join(f"{k} {v}개({self.bytes.get(k, 0) / 1e6:.1f}MB)" for k, v in sorted(self.removed.items()))


def safe_unlink(paths: Paths, p: Path, report: Report, cat: str, dry: bool = False) -> None:
    """보호 경로(미디어 원본) 아래는 절대 지우지 않는다."""
    for root in paths.protected_roots:
        if is_within(p, root):
            raise ProtectedPathError(f"자동 정리가 미디어 원본을 지우려 함 — 거부: {p}")
    try:
        size = p.lstat().st_size
        if not dry:
            p.unlink()
        report.add(cat, size)
        log.debug("삭제[%s] %s (%d bytes)%s", cat, p, size, " (dry-run)" if dry else "")
    except FileNotFoundError:
        pass


def _older_than(p: Path, seconds: float, now: float) -> bool:
    try:
        return now - p.lstat().st_mtime > seconds
    except FileNotFoundError:
        return False


def _remove_empty_dirs(root: Path, keep_root: bool = True) -> None:
    if not root.is_dir():
        return
    for d in sorted((x for x in root.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def clean_thumbs(paths: Paths, rep: Report, dry: bool) -> None:
    base = paths.thumbs_dir / "video"
    if not base.is_dir():
        return
    for t in base.rglob("*.jpg"):
        folder = t.parent.name
        src = paths.video / folder / t.name[:-4]  # 'clip.mp4.jpg' → 'clip.mp4'
        if not src.exists():
            safe_unlink(paths, t, rep, "고아 썸네일", dry)
    _remove_empty_dirs(base)


def clean_concat(paths: Paths, rep: Report, dry: bool, now: float) -> None:
    base = paths.concat_dir
    if not base.is_dir():
        return
    today = time.strftime("%Y%m%d", time.gmtime(now))
    for live_dir in base.iterdir():
        if not live_dir.is_dir():
            continue
        cur = live_dir / "current.ffconcat"
        keep: set[str] = set()
        if cur.is_symlink():
            target = os.readlink(cur)
            keep |= {target, target.replace(".ffconcat", ".json")}
        for f in live_dir.iterdir():
            if f.name in keep or f.name == "current.ffconcat" or f.name.startswith(today):
                continue
            if f.suffix in (".ffconcat", ".json", ".tmp"):
                safe_unlink(paths, f, rep, "지난 concat 목록", dry)


def clean_incoming(paths: Paths, rep: Report, dry: bool, now: float, hours: float) -> None:
    limit = hours * 3600
    for p in paths.incoming.rglob("*") if paths.incoming.is_dir() else []:
        if p.is_file() and _older_than(p, limit, now):
            safe_unlink(paths, p, rep, "중단된 업로드", dry)


def clean_rejected(paths: Paths, rep: Report, dry: bool, now: float, days: float) -> None:
    limit = days * 86400
    if not paths.rejected.is_dir():
        return
    for p in paths.rejected.rglob("*"):
        if p.is_file() and _older_than(p, limit, now):
            safe_unlink(paths, p, rep, "격리 파일", dry)
    _remove_empty_dirs(paths.rejected)


def _trim_file(p: Path, keep: int) -> int:
    """기록 중인 로그를 끝부분 keep 바이트만 남기고 줄인다 (같은 파일에 덮어씀).
    쓰는 쪽(Python·Liquidsoap·stunnel·Caddy)은 모두 추가 모드라 줄인 뒤에도 끝에 이어 쓴다. 줄인 바이트 수."""
    size = p.stat().st_size
    if size <= keep:
        return 0
    with open(p, "r+b") as f:
        f.seek(size - keep)
        data = f.read()
        nl = data.find(b"\n")
        data = "[... LiveMax: 오래된 기록 정리됨 ...]\n".encode() + data[nl + 1 if nl >= 0 else 0:]
        f.seek(0)
        f.write(data)
        f.truncate(len(data))
    return size - len(data)


def clean_logs(paths: Paths, rep: Report, dry: bool, now: float, days: float, max_mb: float = 10,
               keep_mb: float = 2, total_mb: float = 300) -> None:
    """컨테이너에는 logrotate 가 없다 → 직접 크기를 묶는다.
    - 파일 하나가 max_mb 를 넘으면 마지막 keep_mb 만 남김 (매시간)
    - logs 폴더 전체가 total_mb 를 넘으면 큰 파일부터 keep_mb/2 로 줄임
    - 기록이 끝난 파일(*.log 가 아닌 것, 지운 라이브의 로그)은 기간이 지나면 삭제"""
    if not paths.logs.is_dir():
        return
    for p in paths.logs.iterdir():
        if p.is_file() and not p.name.endswith(".log") and _older_than(p, days * 86400, now):
            safe_unlink(paths, p, rep, "오래된 로그", dry)
    logs = [p for p in paths.logs.glob("*.log") if p.is_file()]
    for p in logs:
        if p.stat().st_size > max_mb * 1e6 and not dry:
            rep.add("로그 줄임", _trim_file(p, int(keep_mb * 1e6)))
    total = sum(p.stat().st_size for p in logs)
    if total > total_mb * 1e6 and not dry:
        for p in sorted(logs, key=lambda x: x.stat().st_size, reverse=True):
            rep.add("로그 줄임(전체 한도)", _trim_file(p, int(keep_mb * 1e6 / 2)))
            total = sum(x.stat().st_size for x in logs)
            if total <= total_mb * 1e6 * 0.7:
                break


def clean_runtime(paths: Paths, rep: Report, dry: bool, live_ids: set[str]) -> None:
    if not paths.run_dir.is_dir():
        return
    for p in paths.run_dir.iterdir():
        lid = p.name.split(".", 1)[0]
        if p.is_file() and lid not in live_ids and lid != "system":
            safe_unlink(paths, p, rep, "런타임 찌꺼기", dry)


def rollup_metrics(conn, raw_days: float, now: float, dry: bool) -> int:
    """원본 지표 중 raw_days 보다 오래된 것을 시간 단위로 집계한 뒤 원본 삭제.

    하루치씩 나눠 짧은 트랜잭션으로 처리한다 — 한 번에 수십만 행을 지우면 그동안 DB 쓰기가 막힌다."""
    cutoff = now - raw_days * 86400
    if dry:
        return conn.execute("SELECT COUNT(*) FROM metrics_system WHERE ts < ?", (cutoff,)).fetchone()[0]
    oldest = conn.execute("SELECT MIN(ts) FROM (SELECT MIN(ts) AS ts FROM metrics_system "
                          "UNION ALL SELECT MIN(ts) FROM metrics_live)").fetchone()[0]
    n = 0
    start = (int(oldest) // 3600) * 3600 if oldest else cutoff
    while start < cutoff:
        end = min(start + 86400, (int(cutoff) // 3600) * 3600) if start + 3600 <= cutoff else cutoff
        if end <= start:
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT OR REPLACE INTO metrics_system_hourly
                   SELECT CAST(ts/3600 AS INTEGER)*3600, AVG(cpu_pct), MAX(cpu_pct), AVG(steal_pct), MAX(steal_pct),
                          AVG(mem_used_mb), MAX(disk_used_gb), AVG(net_tx_mbps), MAX(net_tx_mbps), MAX(month_tx_gb),
                          MAX(lives_running), COUNT(*)
                   FROM metrics_system WHERE ts >= ? AND ts < ? GROUP BY CAST(ts/3600 AS INTEGER)""", (start, end))
            conn.execute(
                """INSERT OR REPLACE INTO metrics_live_hourly(hour_ts, live_id, speed_avg, speed_min, cpu_avg, rss_max,
                                                              samples, kbps_out_min, delay_max)
                   SELECT CAST(ts/3600 AS INTEGER)*3600, live_id, AVG(speed), MIN(speed), AVG(cpu_pct), MAX(rss_mb), COUNT(*),
                          MIN(kbps_out), MAX(delay_s)
                   FROM metrics_live WHERE ts >= ? AND ts < ? GROUP BY CAST(ts/3600 AS INTEGER), live_id""", (start, end))
            n += conn.execute("DELETE FROM metrics_system WHERE ts >= ? AND ts < ?", (start, end)).rowcount
            conn.execute("DELETE FROM metrics_live WHERE ts >= ? AND ts < ?", (start, end))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        start = end
        time.sleep(0.05)  # 다른 쓰기(지표·이벤트)가 끼어들 틈
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM events WHERE ts < ?", (now - 30 * 86400,))
        conn.execute("DELETE FROM login_attempts WHERE ts < ?", (now - 30 * 86400,))
        conn.execute("DELETE FROM rejections WHERE ts < ?", (now - 30 * 86400,))
        conn.execute("DELETE FROM restarts WHERE ts < ?", (now - 30 * 86400,))
        conn.execute("DELETE FROM pending_deletes")  # v2.1 에서 없앤 기능의 남은 행
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute("DELETE FROM metrics_system_hourly WHERE hour_ts < ?", (now - 400 * 86400,))
        conn.execute("DELETE FROM metrics_live_hourly WHERE hour_ts < ?", (now - 400 * 86400,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


def compact_db(paths: Paths, conn, max_mb: float, now: float) -> str | None:
    """DB 가 max_mb 를 넘으면 원본 지표를 더 짧게(12시간) 남기고, 빈 공간이 많으면 VACUUM 으로 파일을 줄인다."""
    size = paths.db.stat().st_size if paths.db.exists() else 0
    msg = None
    if size > max_mb * 1e6:
        rollup_metrics(conn, 0.5, now, False)
        msg = f"DB {size / 1e6:.0f}MB > {max_mb:.0f}MB — 원본 지표를 12시간만 남김"
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    # VACUUM 은 도는 동안 DB 쓰기를 막는다 → 빈 공간이 절반을 넘을 때만 (보통 처음 업데이트 직후 1번)
    if pages and free / pages > 0.5 and size > 20e6:
        conn.execute("VACUUM")
        after = paths.db.stat().st_size
        msg = (msg + " · " if msg else "") + f"DB 압축 {size / 1e6:.0f}→{after / 1e6:.0f}MB"
    return msg


def clean_uploads(paths: Paths, rep: Report, dry: bool, now: float, hours: float) -> None:
    """끊긴 웹 업로드 조각 (state/uploads) — 24시간 지나면 삭제."""
    if not paths.uploads.is_dir():
        return
    for p in paths.uploads.iterdir():
        if p.is_file() and _older_than(p, hours * 3600, now):
            safe_unlink(paths, p, rep, "중단된 업로드", dry)


def run(paths: Paths, settings: dict, emergency: bool = False, dry: bool = False, manual: bool = False) -> Report:
    cl = settings["cleanup"]
    now = time.time()
    rep = Report()
    conn = db.connect(paths.db)
    rejected_days = 0 if emergency else float(cl["rejected_days"])
    log_days = 2 if emergency else float(cl["log_days"])
    incoming_h = 6 if emergency else float(cl["incoming_stale_hours"])
    mode = "긴급" if emergency else ("수동" if manual else "정기")
    log.info("정리 시작 (%s%s)", mode, ", dry-run" if dry else "")
    lives = LiveStore(paths, settings).all()
    steps = [
        ("썸네일", lambda: clean_thumbs(paths, rep, dry)),
        ("concat", lambda: clean_concat(paths, rep, dry, now)),
        ("업로드 임시", lambda: clean_incoming(paths, rep, dry, now, incoming_h)),
        ("웹 업로드 조각", lambda: clean_uploads(paths, rep, dry, now, incoming_h)),
        ("격리", lambda: clean_rejected(paths, rep, dry, now, rejected_days)),
        ("로그", lambda: clean_logs(paths, rep, dry, now, log_days, float(cl["log_max_mb"]), float(cl["log_keep_mb"]),
                                   float(cl["logs_total_mb"]))),
        ("런타임", lambda: clean_runtime(paths, rep, dry, {lv.id for lv in lives})),
    ]
    for name, fn in steps:
        try:
            fn()
        except ProtectedPathError:
            log.critical("보호 경로 삭제 시도 차단 — 정리 중단 (%s)", name)
            raise
        except Exception:  # noqa: BLE001
            log.exception("정리 단계 실패: %s", name)
    try:
        n = rollup_metrics(conn, float(cl["metrics_raw_days"]), now, dry)
        if n:
            log.info("지표 축약: 원본 %d행", n)
        if not dry:
            m = compact_db(paths, conn, float(cl["db_max_mb"]), now)
            if m:
                log.info(m)
    except Exception:  # noqa: BLE001
        log.exception("지표 축약 실패")
    msg = f"정리 완료 ({mode}): {rep.summary()}"
    log.info(msg)
    # 매시간 돌므로 이벤트(알림 목록)에는 사람이 누른 정리·긴급 정리·큰 정리만 남긴다
    if not dry and (manual or emergency or sum(rep.bytes.values()) > 100e6):
        db.add_event(conn, "info", "cleanup", msg)
    conn.close()
    return rep
