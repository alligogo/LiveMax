"""livemaxd — 컨테이너 안에서 도는 단 하나의 관리 프로세스.

  웹 UI/API (uvicorn, 127.0.0.1:8080 — 외부는 Caddy HTTPS 가 프록시)
  supervisor : 라이브별 Liquidsoap / stunnel / ffmpeg 관리, 감시, 일일 재시작
  ingest     : _incoming 검증 → 방송 폴더로 원자적 이동
  collector  : 시스템·라이브 지표
  cleanup    : 하루 1회 정리 (+ 긴급·수동)
  notify     : Pushbullet·텔레그램 모바일 알림 (디스크·재부팅·업로드 거부 감시 포함)
  telegram   : 텔레그램 봇 명령 받기 (/상태 /오늘 /라이브 N — 읽기 전용)
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from . import channels, cleanup, config, cpuwatch, db, fetcher, ingest, logsetup, notify, prefetch, sysmetrics, thumbs, updatecheck
from .collector import Collector
from .paths import get_paths
from .supervisor import Supervisor

log = logging.getLogger("livemax.daemon")
SWAP_IO_ALERT_PPS = 50  # 스왑 활동 알림 기준: 초당 페이지(4KB) 수, 3분 연속


class App:
    def __init__(self):
        self.paths = get_paths()
        self.paths.ensure()
        self.settings = config.load_settings(self.paths)
        logsetup.setup("livemaxd", self.paths.logs, self.settings["logging"]["level"])
        db.init(self.paths.db)
        try:  # v2.8: 시작한 적 없이 '켜짐'으로 저장된 라이브(새 라이브 저장 버그)를 대기중으로 — 부팅 때 저절로 시작되지 않게
            flag = self.paths.state / "migrations" / "v28-never-started"
            if not flag.exists():
                store = config.LiveStore(self.paths, self.settings)
                for lv in store.all():
                    if lv.enabled and not (self.paths.run_dir / f"{lv.id}.session.json").exists():
                        lv.enabled = False
                        store.save(lv)
                        log.info("%s: 시작한 적 없는 라이브를 대기중으로 바로잡음", lv.id)
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text("done")
        except Exception:  # noqa: BLE001
            log.exception("v2.8 라이브 상태 정리 실패")
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
        self.fetcher = fetcher.Fetcher(self.paths, self.settings)

    # ---------------------------------------------------------------- 백그라운드 작업
    def _ingest_loop(self) -> None:
        try:  # 업로드 검사·썸네일·폴더 감시는 송출보다 낮은 우선순위로 (리눅스는 이 스레드와 자식 프로세스에만 적용)
            os.nice(15)
        except OSError:
            pass
        conn = db.connect(self.paths.db)
        seen: dict = {}
        memo: dict = {}
        last_watch = 0.0
        while not self.stop.wait(float(self.settings["ingest"]["poll_seconds"])):
            try:
                ingest.scan_once(self.paths, self.settings, conn, seen)
            except Exception:  # noqa: BLE001
                log.exception("ingest 실패")
            if time.time() - last_watch >= 60:  # 방송 폴더에 직접 넣거나 바꾼 파일 감시 (1분마다)
                last_watch = time.time()
                try:
                    ingest.watch_broadcast(self.paths, self.settings, conn, memo)
                except Exception:  # noqa: BLE001
                    log.exception("방송 폴더 감시 실패")

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
        swap_prev, swap_hot = sysmetrics.read_swap_io(), 0
        while not self.stop.wait(60):
            try:
                # 스왑을 계속 오가는 상태 = 안 쓰던 메모리를 다시 자꾸 꺼내 씀 = 메모리가 정말 모자람 (방송이 느려질 수 있음)
                # 한 번 옮겨 둔 것(스왑 사용량)은 괜찮고, 계속 오가는 것(활동량)만 본다. 초당 50페이지(약 200KB) 넘게 3분 연속이면 알림
                cur = sysmetrics.read_swap_io()
                rate, swap_prev = (cur - swap_prev) / 60.0, cur
                swap_hot = swap_hot + 1 if rate >= SWAP_IO_ALERT_PPS else 0
                if swap_hot == 3:
                    msg = (f"메모리 부족 — 스왑을 계속 쓰는 중 (초당 {rate:.0f}페이지). 방송이 느려질 수 있습니다. "
                           "라이브 수를 줄이거나 메모리를 많이 쓰는 라이브를 재시작하세요")
                    log.warning(msg)
                    db.add_event(conn, "error", "system", msg)
                    n.send("system", "메모리 부족 (스왑 사용 중)", msg, dedupe_s=3600)
                pct = self.collector.latest.get("disk_pct")
                if pct and pct >= float(self.settings["cleanup"]["disk_warn_percent"]):
                    n.send("system", f"디스크 {pct:.0f}% 사용 중", "비디오·음악 파일을 정리하세요", dedupe_s=6 * 3600)
                if flag.exists():
                    n.send("system", "서버 재부팅 필요", "보안 업데이트 적용 대기 — 한가한 시간에 sudo reboot", dedupe_s=7 * 86400)
                for e in conn.execute("SELECT id, source, message FROM events WHERE id > ? AND source IN ('ingest','media-watch') "
                                      "AND level='warn' ORDER BY id", (last_ev,)).fetchall():
                    if e["source"] == "ingest":
                        n.send("media", "업로드 파일 거부", e["message"])
                    else:
                        n.send("system", "방송 폴더 직접 변경", e["message"])
                last_ev = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()["m"] or last_ev
            except Exception:  # noqa: BLE001
                log.exception("알림 감시 실패")

    def start_background(self) -> None:
        for name, target in (("supervisor", self.supervisor.loop), ("ingest", self._ingest_loop),
                             ("collector", lambda: self.collector.run(self.stop)), ("cleanup", self._cleanup_loop),
                             ("notify-watch", self._notify_loop), ("updatecheck", lambda: self.updates.run(self.stop)),
                             ("boot", self.supervisor.start_all), ("cpuwatch", lambda: cpuwatch.run(self.stop)),
                             ("fetch", lambda: self.fetcher.run(self.stop)),
                             ("prefetch", lambda: prefetch.run(self.supervisor, self.stop)),
                             ("telegram", lambda: self.notifier.run_bot(self.stop))):
            threading.Thread(target=target, name=name, daemon=True).start()
        log.info("livemaxd %s 시작 (data=%s)", self.version, self.paths.root)

        def _gc_later():  # 라이브들이 순서대로 다 올라온 뒤 (부팅 순차 시작이 끝날 즈음)
            if not self.stop.wait(180):
                cpuwatch.tune_gc()
                log.info("메모리 청소 조정 완료 (시작 때 객체 제외)")
        threading.Thread(target=_gc_later, name="gc-tune", daemon=True).start()

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
