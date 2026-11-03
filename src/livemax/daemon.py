"""livemaxd — 컨테이너 안에서 도는 단 하나의 관리 프로세스.

  웹 UI/API (uvicorn, 127.0.0.1:8080 — 외부는 Caddy HTTPS 가 프록시)
  supervisor : 라이브별 Liquidsoap / stunnel / ffmpeg 관리, 감시, 일일 재시작
  ingest     : _incoming 검증 → 방송 폴더로 원자적 이동
  collector  : 시스템·라이브 지표
  cleanup    : 하루 1회 정리 (+ 긴급·수동)
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

from . import cleanup, config, db, ingest, logsetup
from .collector import Collector
from .paths import get_paths
from .supervisor import Supervisor

log = logging.getLogger("livemax.daemon")


class App:
    def __init__(self):
        self.paths = get_paths()
        self.paths.ensure()
        self.settings = config.load_settings(self.paths)
        logsetup.setup("livemaxd", self.paths.logs, self.settings["logging"]["level"])
        db.init(self.paths.db)
        self.stop = threading.Event()
        self.supervisor = Supervisor(self.paths, self.settings)
        self.collector = Collector(self.paths, self.settings, self.supervisor, on_emergency=self.cleanup_async)
        self.started_at = time.time()
        self._cleanup_lock = threading.Lock()
        self.version = os.environ.get("LIVEMAX_VERSION", "dev")

    # ---------------------------------------------------------------- 백그라운드 작업
    def _ingest_loop(self) -> None:
        conn = db.connect(self.paths.db)
        seen: dict = {}
        while not self.stop.wait(float(self.settings["ingest"]["poll_seconds"])):
            try:
                ingest.scan_once(self.paths, self.settings, conn, seen)
            except Exception:  # noqa: BLE001
                log.exception("ingest 실패")

    def cleanup_async(self, emergency: bool = False, manual: bool = False) -> bool:
        if not self._cleanup_lock.acquire(blocking=False):
            return False

        def run():
            try:
                os.nice(10)
            except OSError:
                pass
            try:
                cleanup.run(self.paths, self.settings, emergency=emergency, manual=manual)
            except Exception:  # noqa: BLE001
                log.exception("정리 실패")
            finally:
                self._cleanup_lock.release()

        threading.Thread(target=run, name="cleanup", daemon=True).start()
        return True

    def _cleanup_loop(self) -> None:
        done = ""
        while not self.stop.wait(60):
            now = datetime.now(timezone.utc)
            if now.strftime("%H:%M") == self.settings["cleanup"]["time_utc"] and done != now.date().isoformat():
                done = now.date().isoformat()
                self.cleanup_async()

    def start_background(self) -> None:
        for name, target in (("supervisor", self.supervisor.loop), ("ingest", self._ingest_loop),
                             ("collector", lambda: self.collector.run(self.stop)), ("cleanup", self._cleanup_loop),
                             ("boot", self.supervisor.start_all)):
            threading.Thread(target=target, name=name, daemon=True).start()
        log.info("livemaxd %s 시작 (data=%s)", self.version, self.paths.root)

    def shutdown(self) -> None:
        log.info("종료 중 — 라이브 정지")
        self.stop.set()
        self.supervisor.shutdown()


def main() -> None:
    import uvicorn

    from .web.api import create_app

    app_state = App()
    api = create_app(app_state)
    app_state.start_background()

    def _term(*_):
        app_state.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    d = app_state.settings["dashboard"]
    uvicorn.run(api, host=d["bind"], port=int(d["port"]), proxy_headers=False, access_log=False, log_config=None)
    app_state.shutdown()


if __name__ == "__main__":
    main()
