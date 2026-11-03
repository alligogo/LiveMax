"""계획된 일일 재시작 — livemax-scheduler.service (DESIGN 4.4, 6.1)

장애를 막으려 하기보다 터지기 전에 미리 새로 띄운다. 라이브별 restart_at(라이브 timezone 기준)에
live@<id> 만 재시작한다. 재시작된 runner 는 그 시점부터 24시간치 concat 목록을 새로 만들므로
"목록 갱신 = 재시작 하루 1회"가 되고 PTS 드리프트·메모리 누수·로그 누적도 함께 리셋된다.

동시 재시작 절대 금지 (DESIGN 11-6):
  - 설정 검증(config.cross_check)이 30초 이내로 겹치는 restart_at 을 거부하고,
  - 여기서도 실행 시점에 전역 간격(min_gap_seconds)을 강제한다. 겹치면 큐에서 기다린다.

설정 파일을 매 루프 다시 읽으므로 라이브 추가·삭제 시 이 서비스도 재시작할 필요가 없다.
"""

from __future__ import annotations

import logging
import signal
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from . import config, db, logsetup, systemctl
from .state import read_json, write_json_atomic

log = logging.getLogger("livemax.scheduler")
LATE_WINDOW = timedelta(minutes=30)  # 서버가 잠깐 꺼져 있었으면 30분까지는 늦게라도 실행


def due_lives(lives: list[config.LiveConfig], done: dict[str, str], now: datetime) -> list[tuple[datetime, str]]:
    out = []
    for lv in lives:
        if not lv.enabled:
            continue
        local_day = now.astimezone(lv.tz).date()
        for day in (local_day - timedelta(days=1), local_day):
            inst = lv.restart_instant_utc(day)
            key = day.isoformat()
            if inst <= now < inst + LATE_WINDOW and done.get(lv.id) != key:
                out.append((inst, lv.id, key))
    out.sort()
    return [(i, f"{lid}|{k}") for i, lid, k in out]


def main() -> int:
    server = config.load_server()
    paths = config.get_paths(server)
    logsetup.setup("scheduler", server)
    db.init(paths.db)
    conn = db.connect(paths.db)
    state_file = paths.state_dir / "scheduler.json"
    st = read_json(state_file, {}) or {}
    done: dict[str, str] = st.get("done", {})
    last_restart = float(st.get("last_restart", 0))
    queue: deque[str] = deque()

    stop = False

    def _sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log.info("scheduler 시작")
    last_err_report = ""
    last_cross = 0.0

    while not stop:
        try:
            server = config.load_server()
            gap = float(server["restart"]["min_gap_seconds"])
            do_cross = time.time() - last_cross >= 300  # 1년치 겹침 검사는 5분마다
            lives, errors = config.load_all_lives(server=server, cross=do_cross)
            if do_cross:
                last_cross = time.time()
                err_text = "; ".join(f"{k}: {v.strip()}" for k, v in errors.items())
                if err_text and err_text != last_err_report:
                    log.error("설정 오류: %s", err_text)
                    db.add_event(conn, "error", "scheduler", f"설정 오류: {err_text}")
                last_err_report = err_text

            now = datetime.now(timezone.utc)
            for _inst, token in due_lives(lives, done, now):
                if token not in queue:
                    queue.append(token)
                    log.info("재시작 예약: %s", token)

            if queue and time.time() - last_restart >= gap:
                token = queue.popleft()
                live_id, day = token.split("|")
                ok = systemctl.restart(systemctl.video_unit(live_id))
                last_restart = time.time()
                done[live_id] = day
                write_json_atomic(state_file, {"done": done, "last_restart": last_restart})
                conn.execute("INSERT INTO restarts(ts, live_id, reason, source) VALUES (?,?,?,?)",
                             (last_restart, live_id, "planned daily restart", "scheduler"))
                db.add_event(conn, "info" if ok else "error", "scheduler",
                             f"일일 재시작 {'요청' if ok else '실패'} ({day})", live_id)
                log.info("일일 재시작 %s %s (대기열 %d)", live_id, "OK" if ok else "FAIL", len(queue))
        except Exception:  # noqa: BLE001
            log.exception("scheduler 루프 오류")
        for _ in range(50):
            if stop:
                break
            time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
