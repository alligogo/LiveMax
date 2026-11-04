"""livemaxd — 컨테이너 안에서 도는 단 하나의 관리 프로세스.

  웹 UI/API (uvicorn, 127.0.0.1:8080 — 외부는 Caddy HTTPS 가 프록시)
  supervisor : 라이브별 Liquidsoap / stunnel / ffmpeg 관리, 감시, 일일 재시작
  ingest     : _incoming 검증 → 방송 폴더로 원자적 이동
  collector  : 시스템·라이브 지표
  cleanup    : 하루 1회 정리 (+ 긴급·수동)
  notify     : Pushbullet 모바일 알림 (디스크·재부팅·업로드 거부 감시 포함)
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from . import channels, cleanup, config, db, ingest, logsetup, notify, thumbs, updatecheck
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
        try:  # v2.4: 라이브별 키 → 채널 키 (1회)
            msg = channels.migrate_legacy(self.paths, self.settings)
            if msg:
                with db.session(self.paths.db) as c:
                    db.add_event(c, "info", "channels", msg)
        except Exception:  # noqa: BLE001
            log.exception("채널 이전 실패 — 기존 키로 계속 송출")
        self.stop = threading.Event()
        self.supervisor = Supervisor(self.paths, self.settings)
        self.notifier = notify.Notifier(self.paths, self.settings)
        self.supervisor.notify = self.notifier.send
        self.collector = Collector(self.paths, self.settings, self.supervisor, on_emergency=self.cleanup_async)
        self.started_at = time.time()
        self._cleanup_lock = threading.Lock()
        self.version = os.environ.get("LIVEMAX_VERSION", "dev")
        self.updates = updatecheck.UpdateChecker(self.version)

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
        """시작 5분 뒤부터 매시간 정리 (로그 크기·DB·임시 파일·고아 썸네일 등). 예전 원본 크기 썸네일은 시작 때 1회 축소."""
        try:
            os.nice(10)
        except OSError:
            pass
        if self.stop.wait(30):
            return
        try:
            thumbs.shrink_existing(self.paths)
        except Exception:  # noqa: BLE001
            log.exception("썸네일 축소 실패")
        wait = 300.0
        while not self.stop.wait(wait):
            self.cleanup_async()
            wait = float(self.settings["cleanup"].get("interval_minutes", 60)) * 60

    def _notify_loop(self) -> None:
        """라이브 밖의 알림거리: LiveMax 시작, 디스크 부족, 재부팅 필요, 업로드 검증 실패."""
        n = self.notifier
        if not self.stop.wait(20):
            n.send("system", f"LiveMax 시작 (버전 {self.version})", "서버 또는 LiveMax 가 다시 시작되었습니다. 라이브는 순서대로 복귀합니다")
        flag = self.paths.state / "host" / "reboot-required"
        conn = db.connect(self.paths.db)
        last_ev = (conn.execute("SELECT MAX(id) AS m FROM events").fetchone()["m"] or 0)
        while not self.stop.wait(60):
            try:
                pct = self.collector.latest.get("disk_pct")
                if pct and pct >= float(self.settings["cleanup"]["disk_warn_percent"]):
                    n.send("system", f"디스크 {pct:.0f}% 사용 중", "비디오·음악 파일을 정리하세요", dedupe_s=6 * 3600)
                if flag.exists():
                    n.send("system", "서버 재부팅 필요", "보안 업데이트 적용 대기 — 한가한 시간에 sudo reboot", dedupe_s=7 * 86400)
                for e in conn.execute("SELECT id, message FROM events WHERE id > ? AND source='ingest' AND level='warn' "
                                      "ORDER BY id", (last_ev,)).fetchall():
                    n.send("media", "업로드 파일 거부", e["message"])
                last_ev = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()["m"] or last_ev
            except Exception:  # noqa: BLE001
                log.exception("알림 감시 실패")

    def start_background(self) -> None:
        for name, target in (("supervisor", self.supervisor.loop), ("ingest", self._ingest_loop),
                             ("collector", lambda: self.collector.run(self.stop)), ("cleanup", self._cleanup_loop),
                             ("notify-watch", self._notify_loop), ("updatecheck", lambda: self.updates.run(self.stop)),
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
