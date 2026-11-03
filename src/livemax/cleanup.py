"""자동 정리 + 디스크 위생 — livemax-cleanup.service (+ .timer, 하루 1회) (DESIGN 6.4)

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


def clean_logs(paths: Paths, rep: Report, dry: bool, now: float, days: float) -> None:
    if not paths.logs.is_dir():
        return
    for p in paths.logs.iterdir():
        # 현재 기록 중인 *.log 는 logrotate 담당. 회전된 *.log.N / *.gz 만 기간 초과 시 삭제
        if p.is_file() and not p.name.endswith(".log") and _older_than(p, days * 86400, now):
            safe_unlink(paths, p, rep, "오래된 로그", dry)


def clean_runtime(paths: Paths, rep: Report, dry: bool, live_ids: set[str]) -> None:
    if not paths.run_dir.is_dir():
        return
    for p in paths.run_dir.iterdir():
        lid = p.name.split(".", 1)[0]
        if p.is_file() and lid not in live_ids and lid != "system":
            safe_unlink(paths, p, rep, "런타임 찌꺼기", dry)


def rollup_metrics(conn, raw_days: float, now: float, dry: bool) -> int:
    """원본 30일 초과분을 시간 단위로 집계한 뒤 원본 삭제."""
    cutoff = now - raw_days * 86400
    if dry:
        return conn.execute("SELECT COUNT(*) FROM metrics_system WHERE ts < ?", (cutoff,)).fetchone()[0]
    conn.execute("BEGIN")
    try:
        conn.execute(
            """INSERT OR REPLACE INTO metrics_system_hourly
               SELECT CAST(ts/3600 AS INTEGER)*3600, AVG(cpu_pct), MAX(cpu_pct), AVG(steal_pct), MAX(steal_pct),
                      AVG(mem_used_mb), MAX(disk_used_gb), AVG(net_tx_mbps), MAX(net_tx_mbps), MAX(month_tx_gb),
                      MAX(lives_running), COUNT(*)
               FROM metrics_system WHERE ts < ? GROUP BY CAST(ts/3600 AS INTEGER)""", (cutoff,))
        conn.execute(
            """INSERT OR REPLACE INTO metrics_live_hourly
               SELECT CAST(ts/3600 AS INTEGER)*3600, live_id, AVG(speed), MIN(speed), AVG(cpu_pct), MAX(rss_mb), COUNT(*)
               FROM metrics_live WHERE ts < ? GROUP BY CAST(ts/3600 AS INTEGER), live_id""", (cutoff,))
        n = conn.execute("DELETE FROM metrics_system WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM metrics_live WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM events WHERE ts < ?", (now - 90 * 86400,))
        conn.execute("DELETE FROM login_attempts WHERE ts < ?", (now - 90 * 86400,))
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


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
        ("로그", lambda: clean_logs(paths, rep, dry, now, log_days)),
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
    except Exception:  # noqa: BLE001
        log.exception("지표 축약 실패")
    msg = f"정리 완료 ({mode}): {rep.summary()}"
    log.info(msg)
    if not dry:
        db.add_event(conn, "info", "cleanup", msg)
    conn.close()
    return rep
