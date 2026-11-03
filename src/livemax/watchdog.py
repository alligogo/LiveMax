"""워치독 — livemax-watchdog.service (DESIGN 6.2)

단순 프로세스 생존 확인으로는 부족하다. 다음을 감시한다:
  1. ffmpeg speed= 값 — speed_min 미만이 speed_grace_seconds 동안 지속되면 재시작
  2. 출력 정지 — 상태 파일 갱신이 stall_seconds 동안 없으면 재시작
  3. 유튜브 방송 상태 — API 로 'live' 가 아니면 (연속 N회) 재시작
프로세스가 죽는 경우는 systemd Restart=always 가 처리한다.

재시작 폭주 방지: 라이브별 시간당 max_restarts_per_hour 초과 시 재시작 대신 경고만 남긴다.
여러 라이브가 동시에 문제여도 restart_spacing_seconds 간격으로 하나씩 (동시 재시작 금지).
재시작 대상은 문제 라이브의 live@<id> 하나뿐이다. 다른 라이브는 건드리지 않는다.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field

from . import config, db, logsetup, secrets, systemctl, youtube
from .state import read_json

log = logging.getLogger("livemax.watchdog")


@dataclass
class LiveWatch:
    low_speed_since: float | None = None
    yt_fail: int = 0
    yt_last_check: float = 0.0
    last_restart: float = 0.0
    restarts: list[float] = field(default_factory=list)
    last_state: str = ""


def evaluate(status: dict | None, w: LiveWatch, cfg: dict, now: float) -> str | None:
    """재시작이 필요하면 사유 문자열, 아니면 None. (순수 함수 — 테스트 가능)"""
    if not status:
        return None  # 상태 파일이 없으면 아직 시작 전이거나 systemd 가 처리 중
    state = status.get("state")
    if state != "running":
        w.low_speed_since = None
        return None
    started = float(status.get("ffmpeg_started_at") or status.get("started_at") or now)
    if now - started < float(cfg["startup_grace_seconds"]):
        return None
    updated = float(status.get("updated_at") or 0)
    if now - updated > float(cfg["stall_seconds"]):
        return f"출력 정지 {now - updated:.0f}초 (상태 갱신 없음)"
    speed = status.get("speed")
    if speed is not None and float(speed) < float(cfg["speed_min"]):
        if w.low_speed_since is None:
            w.low_speed_since = now
        elif now - w.low_speed_since >= float(cfg["speed_grace_seconds"]):
            return f"speed {float(speed):.3f}x 가 {now - w.low_speed_since:.0f}초 지속"
    else:
        w.low_speed_since = None
    return None


def main() -> int:
    server = config.load_server()
    paths = config.get_paths(server)
    logsetup.setup("watchdog", server)
    db.init(paths.db)
    conn = db.connect(paths.db)
    watches: dict[str, LiveWatch] = {}
    last_any_restart = 0.0

    stop = False

    def _sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log.info("watchdog 시작")

    while not stop:
        try:
            server = config.load_server()
            cfg = server["watchdog"]
            lives, _errors = config.load_all_lives(server=server, cross=False)
            api_key = ""
            kf = config.resolve_home_path(server["youtube"]["api_key_file"])
            if kf.exists():
                api_key = secrets.read_secret(kf)

            now = time.time()
            for lv in lives:
                if not lv.enabled:
                    continue
                w = watches.setdefault(lv.id, LiveWatch())
                st = read_json(paths.status_file(lv.id))
                state = (st or {}).get("state", "unknown")
                if state != w.last_state:
                    log.info("%s 상태 변화: %s → %s", lv.id, w.last_state or "-", state)
                    w.last_state = state

                reason = evaluate(st, w, cfg, now)

                # 유튜브 상태 (선택)
                if reason is None and api_key and lv.youtube_video_id and state == "running" \
                        and now - w.yt_last_check >= float(cfg["youtube_check_seconds"]):
                    w.yt_last_check = now
                    ys = youtube.live_status(lv.youtube_video_id, api_key)
                    if ys is not None:
                        log.debug("%s youtube: %s", lv.id, ys)
                        if ys["live"]:
                            w.yt_fail = 0
                        else:
                            w.yt_fail += 1
                            db.add_event(conn, "warn", "watchdog", f"유튜브 방송 상태 '{ys['content']}' ({w.yt_fail}회)", lv.id)
                            if w.yt_fail >= int(cfg["youtube_fail_threshold"]):
                                reason = f"유튜브 방송이 live 아님 ({ys['content']}, {w.yt_fail}회 연속)"
                                w.yt_fail = 0

                if reason is None:
                    continue

                # 폭주 방지
                w.restarts = [t for t in w.restarts if now - t < 3600]
                if len(w.restarts) >= int(cfg["max_restarts_per_hour"]):
                    log.error("%s 재시작 한도 초과 (%d회/시간) — 재시작 보류: %s", lv.id, len(w.restarts), reason)
                    db.add_event(conn, "error", "watchdog", f"재시작 한도 초과 — 수동 확인 필요: {reason}", lv.id)
                    w.low_speed_since = None
                    continue
                if now - last_any_restart < float(cfg["restart_spacing_seconds"]):
                    continue  # 다음 루프에서 (동시 재시작 금지)

                log.warning("%s 재시작: %s", lv.id, reason)
                ok = systemctl.restart(systemctl.video_unit(lv.id))
                w.restarts.append(now)
                w.last_restart = now
                w.low_speed_since = None
                last_any_restart = now
                conn.execute("INSERT INTO restarts(ts, live_id, reason, source) VALUES (?,?,?,?)",
                             (now, lv.id, reason, "watchdog"))
                db.add_event(conn, "warn" if ok else "error", "watchdog", f"재시작{'' if ok else ' 실패'}: {reason}", lv.id)
        except Exception:  # noqa: BLE001
            log.exception("watchdog 루프 오류")
        for _ in range(int(float(server["watchdog"]["poll_seconds"]) * 10)):
            if stop:
                break
            time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
