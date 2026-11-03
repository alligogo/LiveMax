"""라이브 프로세스 관리자 (v2) — v1 의 systemd 유닛·runner·watchdog·scheduler 를 하나로.

라이브마다 서로 독립된 프로세스 3개:
  audio  : Liquidsoap (폴더 감시·셔플 → AAC copy → 127.0.0.1:<port>)
  relay  : stunnel     (RTMPS TLS, IPv4 고정 — 실패 시 직접 RTMPS)
  video  : ffmpeg      (24시간 concat + 오디오 → -c copy 먹싱 → RTMP(S))
한 라이브의 시작·정지·재시작이 다른 라이브를 건드리지 않는다 (DESIGN 1.3, 11-8).

감시 (DESIGN 6.2): 진행 정지·speed 저하 → 그 라이브의 ffmpeg 만 재시작. 프로세스가 죽으면 백오프 후 재기동.
일일 재시작 (DESIGN 6.1): 라이브별 슬롯 시각, 라이브 간 최소 30초 간격 (11-6).
부팅 시작: 라이브 간 boot_start_gap 간격으로 순차 시작.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import concat, db, ffmpeg_cmd, liquidsoap, secrets, tls_relay
from .config import ConfigError, LiveStore
from .logsetup import UTCFormatter, FMT
from .paths import Paths
from .state import read_json

log = logging.getLogger("livemax.supervisor")
LIQUIDSOAP = "liquidsoap"


def _parse_out_time(v: str) -> float | None:
    try:
        h, m, s = v.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class LiveProc:
    def __init__(self, sup: "Supervisor", live_id: str):
        self.sup = sup
        self.id = live_id
        self.lock = threading.RLock()
        self.desired = False
        self.audio: subprocess.Popen | None = None
        self.video: subprocess.Popen | None = None
        self.relay: tls_relay.Relay | None = None
        self.threads: list[threading.Thread] = []
        self.restarts: deque[tuple[float, str]] = deque(maxlen=50)
        self.relay_fails = 0
        self.relay_block_until = 0.0
        self.low_speed_since: float | None = None
        self.pending_reason: str | None = None
        self.st: dict = {"state": "stopped", "message": "", "speed": None, "out_time": None, "bitrate": None,
                         "ffmpeg_started_at": None, "progress_at": None, "transport": None, "clip": None,
                         "audio_started_at": None}
        self.meta_path: Path | None = None
        self.log = logging.getLogger(f"livemax.live.{live_id}")
        self.log.propagate = False
        if not self.log.handlers:
            fh = logging.handlers.WatchedFileHandler(sup.paths.logs / f"live-{live_id}.log", encoding="utf-8")
            fh.setFormatter(UTCFormatter(FMT))
            fh.addFilter(secrets.Redactor())
            self.log.addHandler(fh)
            self.log.setLevel(logging.INFO)

    # ---------------------------------------------------------------- 상태
    def set(self, **kw) -> None:
        with self.lock:
            self.st.update(kw)

    def snapshot(self) -> dict:
        with self.lock:
            d = dict(self.st)
        d["desired"] = self.desired
        d["video_pid"] = self.video.pid if self.video and self.video.poll() is None else None
        d["audio_pid"] = self.audio.pid if self.audio and self.audio.poll() is None else None
        now = time.time()
        d["restarts_hour"] = sum(1 for t, _ in self.restarts if now - t < 3600)
        d["restarts"] = [{"ts": t, "reason": r} for t, r in list(self.restarts)[-10:]]
        d["nowplaying"] = read_json(self.sup.paths.run_dir / f"{self.id}.nowplaying.json") or {}
        return d

    def event(self, level: str, msg: str) -> None:
        getattr(self.log, {"info": "info", "warn": "warning", "error": "error"}[level])(msg)
        try:
            with db.session(self.sup.paths.db) as c:
                db.add_event(c, level, "live", msg, self.id)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 제어
    def start(self) -> None:
        with self.lock:
            if self.desired:
                return
            self.desired = True
            self.set(state="starting", message="시작 중")
        self.event("info", "시작")
        for target in (self._audio_loop, self._video_loop):
            t = threading.Thread(target=target, name=f"{self.id}-{target.__name__}", daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self, reason: str = "정지") -> None:
        with self.lock:
            self.desired = False
        self._kill_video()
        self._kill(self.audio)
        for t in self.threads:
            t.join(timeout=15)
        self.threads = []
        self.set(state="stopped", message=reason, speed=None, progress_at=None, ffmpeg_started_at=None)
        self.event("info", f"정지 ({reason})")

    def restart(self, reason: str, audio_too: bool = False) -> None:
        """그 라이브만 재시작. audio_too=False 면 영상(ffmpeg)만 — 음악 흐름 유지."""
        self.restarts.append((time.time(), reason))
        self.event("warn" if "정지" in reason or "speed" in reason else "info", f"재시작: {reason}")
        if audio_too:
            self.stop(reason)
            self.start()
            return
        self.pending_reason = reason
        self.set(state="restarting", message=reason)
        self._kill_video()

    @staticmethod
    def _kill(p: subprocess.Popen | None, sig=signal.SIGTERM, wait: float = 8) -> None:
        if p and p.poll() is None:
            p.send_signal(sig)
            try:
                p.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                p.kill()

    def _kill_video(self) -> None:
        self._kill(self.video, signal.SIGINT)
        if self.relay:
            self.relay.stop()

    def _backoff(self, n: int) -> float:
        steps = self.sup.settings["supervisor"]["restart_backoff_seconds"]
        return float(steps[min(n, len(steps) - 1)])

    def _sleep(self, sec: float) -> bool:
        end = time.time() + sec
        while time.time() < end:
            if not self.desired:
                return False
            time.sleep(0.2)
        return self.desired

    # ---------------------------------------------------------------- 오디오
    def _audio_loop(self) -> None:
        fails = 0
        while self.desired:
            live = self.sup.store.get(self.id)
            if not live:
                return
            try:
                liq = liquidsoap.write(live, self.sup.paths, self.sup.settings)
            except Exception as e:  # noqa: BLE001
                self.event("error", f"오디오 스크립트 생성 실패: {e}")
                if not self._sleep(30):
                    return
                continue
            self.log.info("Liquidsoap 시작 (port %d)", live.harbor_port)
            self.audio = subprocess.Popen([LIQUIDSOAP, str(liq)], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set(audio_started_at=time.time())
            started = time.time()
            rc = self.audio.wait()
            if not self.desired:
                return
            fails = 0 if time.time() - started > 120 else fails + 1
            self.event("error", f"오디오(Liquidsoap) 종료 rc={rc} — 재기동")
            if not self._sleep(self._backoff(fails)):
                return

    # ---------------------------------------------------------------- 영상
    def _video_loop(self) -> None:
        fails = 0
        while self.desired:
            ok = self._video_once()
            if not self.desired:
                return
            fails = 0 if ok else fails + 1
            if not self._sleep(self._backoff(fails)):
                return

    def _video_once(self) -> bool:
        """한 번 송출. 정상적으로 오래(5분 이상) 돌았으면 True."""
        paths, settings = self.sup.paths, self.sup.settings
        live = self.sup.store.get(self.id)
        if not live:
            self.desired = False
            return False
        self.set(state="starting", message="시작 중", speed=None, progress_at=None)
        conn = db.connect(paths.db)
        try:
            try:
                list_path = concat.generate(live, paths, conn, settings)
                self.meta_path = list_path.with_suffix(".json")
            except concat.ConcatError as e:
                self.set(state="error", message=str(e))
                self.event("error", str(e))
                return False
            kp = paths.stream_key(live.id)
            if not kp.exists() or not kp.read_text().strip():
                self.set(state="error", message="스트림 키 없음")
                return False
            key = secrets.read_secret(kp)

            # 오디오 준비 대기
            deadline = time.time() + float(settings["output"]["audio_connect_timeout"])
            while time.time() < deadline and self.desired and not _port_open(live.harbor_port):
                time.sleep(0.5)
            if not _port_open(live.harbor_port):
                self.set(state="error", message="오디오 준비 안 됨 (음원 폴더 확인)")
                return False

            base = (live.output_url or settings["output"]["default_url"]).rstrip("/")
            rcfg = settings["output"]["tls_relay"]
            relay_port = None
            if rcfg.get("enabled", True) and time.time() >= self.relay_block_until:
                self.relay = tls_relay.Relay(live.id, paths.run_dir, paths.logs)
                relay_port = self.relay.start(base, verify=bool(rcfg.get("verify", True)),
                                              ipv4=bool(rcfg.get("ipv4", True)))
            out = tls_relay.plan(base, key, relay_port)
            args = ffmpeg_cmd.build(list_path, f"http://127.0.0.1:{live.harbor_port}/{live.id}.aac", out.url,
                                    extra_out=out.extra_args)
            self.log.info("ffmpeg 시작 (%s, %s): %s", out.mode, list_path.name, ffmpeg_cmd.redacted(args, key))
            self.video = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          stdin=subprocess.DEVNULL)
            started = time.time()
            self.low_speed_since = None
            self.set(state="running", message="", transport=out.mode, ffmpeg_started_at=started,
                     progress_at=started, bitrate_cfg=live.bitrate_kbps)
            threading.Thread(target=self._read_stderr, args=(self.video,), daemon=True).start()
            self._read_progress(self.video)
            rc = self.video.wait()
            ran = time.time() - started
            if self.relay:
                self.relay.stop()
            reason, self.pending_reason = self.pending_reason, None
            if out.mode == "rtmps-relay" and ran < 60 and not reason and self.desired:
                self.relay_fails += 1
                if self.relay_fails >= 3:
                    self.relay_block_until = time.time() + 1800
                    self.relay_fails = 0
                    self.event("warn", "TLS 중계 연속 실패 — 30분간 직접 RTMPS 로 송출")
            elif ran >= 60:
                self.relay_fails = 0
            if self.desired and not reason:
                self.event("error", f"ffmpeg 종료 rc={rc} (가동 {int(ran)}초) — 재기동")
                self.set(state="restarting", message=f"ffmpeg 종료 rc={rc}")
            return ran >= 300
        finally:
            conn.close()

    def _read_progress(self, p: subprocess.Popen) -> None:
        block: dict[str, str] = {}
        last_log = 0.0
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            block[k] = v
            if k != "progress":
                continue
            sp = block.get("speed", "").rstrip("x").strip()
            try:
                speed = float(sp) if sp and sp != "N/A" else None
            except ValueError:
                speed = None
            ot = _parse_out_time(block.get("out_time", "")) if block.get("out_time") else None
            br = block.get("bitrate", "")
            try:
                kbps = float(br.replace("kbits/s", "")) if br and br != "N/A" else None
            except ValueError:
                kbps = None
            clip = concat.current_clip(self.meta_path, ot)
            self.set(speed=speed, out_time=ot, bitrate=kbps, progress_at=time.time(),
                     clip=Path(clip["path"]).name if clip else None,
                     clip_folder=Path(clip["path"]).parent.name if clip else None)
            if time.time() - last_log >= 60:
                last_log = time.time()
                self.log.info("progress speed=%s out_time=%s bitrate=%s", speed, block.get("out_time"), br)
            block = {}

    def _read_stderr(self, p: subprocess.Popen) -> None:
        for raw in p.stderr:
            line = secrets.redact(raw.decode("utf-8", "replace").rstrip())
            if line and any(w in line.lower() for w in ("error", "failed", "invalid", "warning")):
                self.log.warning("ffmpeg: %s", line)

    # ---------------------------------------------------------------- 감시
    def watch(self, now: float) -> str | None:
        """재시작이 필요하면 사유."""
        if not self.desired or self.st.get("state") != "running":
            self.low_speed_since = None
            return None
        wd = self.sup.settings["watchdog"]
        started = float(self.st.get("ffmpeg_started_at") or now)
        if now - started < float(wd["startup_grace_seconds"]):
            return None
        last = float(self.st.get("progress_at") or 0)
        if now - last > float(wd["stall_seconds"]):
            return f"출력 정지 {int(now - last)}초"
        sp = self.st.get("speed")
        if sp is not None and sp < float(wd["speed_min"]):
            if self.low_speed_since is None:
                self.low_speed_since = now
            elif now - self.low_speed_since >= float(wd["speed_grace_seconds"]):
                return f"speed {sp:.2f}x 지속"
        else:
            self.low_speed_since = None
        return None


class Supervisor:
    def __init__(self, paths: Paths, settings: dict):
        self.paths = paths
        self.settings = settings
        self.store = LiveStore(paths, settings)
        self.procs: dict[str, LiveProc] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.done_today: dict[str, str] = {}
        self.last_planned = 0.0

    def proc(self, live_id: str) -> LiveProc:
        with self.lock:
            if live_id not in self.procs:
                self.procs[live_id] = LiveProc(self, live_id)
            return self.procs[live_id]

    # ---------------------------------------------------------------- 공개 API
    def start(self, live_id: str) -> None:
        lv = self.store.get(live_id)
        if not lv:
            raise ConfigError("라이브 없음")
        if not self.paths.stream_key(live_id).exists():
            raise ConfigError("스트림 키를 먼저 입력하세요")
        if not lv.enabled:
            lv.enabled = True
            self.store.save(lv)
        self.proc(live_id).start()

    def stop(self, live_id: str) -> None:
        lv = self.store.get(live_id)
        if lv and lv.enabled:
            lv.enabled = False
            self.store.save(lv)
        self.proc(live_id).stop("사용자 정지")

    def restart(self, live_id: str, reason: str = "사용자 재시작", audio_too: bool = True) -> None:
        p = self.proc(live_id)
        if not p.desired:
            self.start(live_id)
            return
        p.restart(reason, audio_too=audio_too)

    def remove(self, live_id: str) -> None:
        with self.lock:
            p = self.procs.pop(live_id, None)
        if p:
            p.stop("삭제")
        self.store.delete(live_id)

    def snapshot(self) -> dict[str, dict]:
        return {lid: p.snapshot() for lid, p in list(self.procs.items())}

    def start_all(self) -> None:
        """부팅: 활성 라이브를 간격을 두고 하나씩 시작 (동시 시작 금지)."""
        gap = float(self.settings["supervisor"]["boot_start_gap_seconds"])
        first = True
        for lv in self.store.all():
            if not lv.enabled or not self.paths.stream_key(lv.id).exists():
                continue
            if not first and self.stop_event.wait(gap):
                return
            first = False
            log.info("부팅 시작: %s", lv.id)
            self.proc(lv.id).start()

    def shutdown(self) -> None:
        self.stop_event.set()
        for p in list(self.procs.values()):
            if p.desired:
                p.desired = False
                p._kill_video()
                p._kill(p.audio)

    # ---------------------------------------------------------------- 주기 작업 (감시 + 일일 재시작)
    def loop(self) -> None:
        while not self.stop_event.wait(5):
            now = time.time()
            wd = self.settings["watchdog"]
            for lid, p in list(self.procs.items()):
                try:
                    reason = p.watch(now)
                    if reason:
                        recent = sum(1 for t, _ in p.restarts if now - t < 3600)
                        if recent >= int(wd["max_restarts_per_hour"]):
                            p.set(message=f"재시작 한도 초과 — {reason}")
                            continue
                        p.restart(reason)
                except Exception:  # noqa: BLE001
                    log.exception("watch 실패 %s", lid)
            self._planned_restarts()

    def _planned_restarts(self) -> None:
        now = datetime.now(timezone.utc)
        gap = 30
        for lv in self.store.all():
            p = self.procs.get(lv.id)
            if not p or not p.desired or p.st.get("state") != "running":
                continue
            h, m = map(int, lv.restart_time_utc(self.settings).split(":"))
            due = now.replace(hour=h, minute=m, second=0, microsecond=0)
            day = now.date().isoformat()
            # 예정 시각부터 30분 안에 한 번 (서버가 잠깐 바빴어도 놓치지 않게)
            if due <= now < due + timedelta(minutes=30) and self.done_today.get(lv.id) != day:
                if time.time() - self.last_planned < gap:
                    return
                self.done_today[lv.id] = day
                self.last_planned = time.time()
                p.restart("일일 재시작")

