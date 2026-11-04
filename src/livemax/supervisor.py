"""라이브 프로세스 관리자 (v2) — v1 의 systemd 유닛·runner·watchdog·scheduler 를 하나로.

라이브마다 서로 독립된 프로세스 3개:
  audio  : Liquidsoap (폴더 감시·셔플 → AAC copy → 127.0.0.1:<port>)
  relay  : stunnel     (RTMPS TLS, IPv4 고정 — 실패 시 직접 RTMPS)
  video  : ffmpeg      (24시간 concat + 오디오 → -c copy 먹싱 → RTMP(S))
한 라이브의 시작·정지·재시작이 다른 라이브를 건드리지 않는다 (DESIGN 1.3, 11-8).

감시 (DESIGN 6.2): 진행 정지·speed 저하 → 그 라이브의 ffmpeg 만 재시작. 프로세스가 죽으면 백오프 후 재기동.
일일 재시작 (DESIGN 6.1): 라이브별 슬롯 시각, 라이브 간 최소 30초 간격 (11-6).
부팅 시작: 라이브 간 boot_start_gap 간격으로 순차 시작.

타이머 (v2.1): "방송 세션" = 사용자가 시작한 뒤 끊김 없이 이어지는 하나의 유튜브 방송.
  세션 시작 시각은 run/<id>.session.json 에 남아 자동 재시작·컨테이너 재시작에도 유지된다.
  종료 시각이 되면 송출을 멈춘다 → 유튜브 '자동 중지'가 방송을 끝내고 다시보기로 저장.
  반복을 켜 두면 timer_gap_minutes 뒤 새 세션(새 유튜브 방송)으로 다시 시작한다.
  타이머 세션 안에서는 일일 재시작을 건너뛴다 (재시작 공백으로 방송이 일찍 끊기지 않게).

재시작 속도: 사용자/감시 재시작은 영상(ffmpeg)만 다시 띄운다 — Liquidsoap 기동(5~7초)을 기다리지 않음.
  의도한 재시작 뒤에는 백오프 없이 바로 다시 띄운다.
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

from . import channels, concat, db, ffmpeg_cmd, liquidsoap, secrets, tls_relay
from .config import ConfigError, LiveStore
from .logsetup import UTCFormatter, FMT
from .paths import Paths
from .state import read_json, write_json_atomic

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
        self.intentional = False  # 마지막 ffmpeg 종료가 의도한 재시작이었나 (백오프 생략)
        self.bad_since: float | None = None  # 송출하려는데 송출이 안 되는 상태가 시작된 시각 (알림용)
        self.down_notified = False
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
        # 정지 상태에서 마지막 비트레이트·곡·클립이 남아 보이지 않게 비운다
        self.set(state="stopped", message=reason, speed=None, progress_at=None, ffmpeg_started_at=None,
                 out_time=None, bitrate=None, clip=None, clip_folder=None, transport=None)
        (self.sup.paths.run_dir / f"{self.id}.nowplaying.json").unlink(missing_ok=True)
        (self.sup.paths.run_dir / f"{self.id}.next.json").unlink(missing_ok=True)
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
            if self.intentional:  # 사용자/감시/일일 재시작 — 기다릴 이유 없음
                self.intentional = False
                continue
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
        self.set(state="starting", message="시작 중", speed=None, progress_at=None, out_time=None, bitrate=None)
        conn = db.connect(paths.db)
        try:
            try:
                list_path = concat.generate(live, paths, conn, settings)
                self.meta_path = list_path.with_suffix(".json")
            except concat.ConcatError as e:
                self.set(state="error", message=str(e))
                self.event("error", str(e))
                return False
            kp = channels.live_key_path(paths, live)
            if not kp.exists() or not kp.read_text().strip():
                self.set(state="error", message="스트림 키 없음")
                return False
            key = secrets.read_secret(kp)

            # 오디오 준비 대기
            deadline = time.time() + float(settings["output"]["audio_connect_timeout"])
            while time.time() < deadline and self.desired and not _port_open(live.harbor_port):
                time.sleep(0.5)
            if not _port_open(live.harbor_port):
                self.set(state="error", message="오디오 준비 안 됨 (음악 폴더 확인)")
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
                     progress_at=started)
            threading.Thread(target=self._read_stderr, args=(self.video,), daemon=True).start()
            self._read_progress(self.video)
            rc = self.video.wait()
            ran = time.time() - started
            if self.relay:
                self.relay.stop()
            reason, self.pending_reason = self.pending_reason, None
            self.intentional = bool(reason)
            if out.mode == "rtmps-relay" and ran < 60 and not reason and self.desired:
                self.relay_fails += 1
                if self.relay_fails >= 3:
                    self.relay_block_until = time.time() + 1800
                    self.relay_fails = 0
                    self.event("warn", "TLS 중계 연속 실패 — 30분간 직접 RTMPS 로 송출")
            elif ran >= 60:
                self.relay_fails = 0
            if self.desired and not reason:
                self.restarts.append((time.time(), f"송출 끊김 rc={rc}"))
                self.event("error", f"ffmpeg 종료 rc={rc} (가동 {int(ran)}초) — 재기동")
                self.sup.notify("restart", f"{live.name}: 송출 프로그램 종료 — 자동 재시작", f"ffmpeg rc={rc}, 가동 {int(ran)}초")
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
        self.last_timer_start = 0.0
        self.last_m3u = 0.0
        self.notify = lambda *a, **k: False  # daemon 이 Notifier.send 로 바꿔 끼운다

    def proc(self, live_id: str) -> LiveProc:
        with self.lock:
            if live_id not in self.procs:
                self.procs[live_id] = LiveProc(self, live_id)
            return self.procs[live_id]

    # ---------------------------------------------------------------- 방송 세션 (타이머 기준)
    def _session_file(self, live_id: str) -> Path:
        return self.paths.run_dir / f"{live_id}.session.json"

    def session(self, live_id: str) -> dict:
        return read_json(self._session_file(live_id)) or {}

    def _write_session(self, live_id: str, **kw) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._session_file(live_id), kw)

    def _clear_session(self, live_id: str) -> None:
        self._session_file(live_id).unlink(missing_ok=True)

    def _new_session(self, live_id: str) -> None:
        now = time.time()
        self._write_session(live_id, started_at=now, alive_at=now)

    def timer_info(self, lv) -> dict:
        """화면용: 세션 시작, 타이머 종료 예정, 다음 방송 시작 예정 (epoch)."""
        ses = self.session(lv.id)
        p = self.procs.get(lv.id)
        live_now = bool(p and p.desired)
        started = ses.get("started_at") if live_now else None
        return {"session_started_at": started,
                "timer_end_at": lv.timer_end_at(started) if live_now else None,
                "next_start_at": ses.get("next_start_at") if not live_now else None}

    # ---------------------------------------------------------------- 공개 API
    def _check_startable(self, lv) -> None:
        if not channels.live_key_path(self.paths, lv).exists():
            raise ConfigError("스트림 키를 먼저 고르세요 (채널 메뉴에서 등록)")
        if lv.timer_mode == "until" and float(lv.timer_until) <= time.time() + 60:
            raise ConfigError("타이머 종료 시각이 이미 지났습니다 — 라이브 설정에서 바꾸세요")

    def start(self, live_id: str, new_session: bool = True) -> None:
        lv = self.store.get(live_id)
        if not lv:
            raise ConfigError("라이브 없음")
        self._check_startable(lv)
        if not lv.enabled:
            lv.enabled = True
            self.store.save(lv)
        p = self.proc(live_id)
        if p.desired:
            return
        if new_session or not self.session(live_id).get("started_at"):
            self._new_session(live_id)
        p.start()

    def stop(self, live_id: str, reason: str = "사용자 정지") -> None:
        lv = self.store.get(live_id)
        if lv and lv.enabled:
            lv.enabled = False
            self.store.save(lv)
        self._clear_session(live_id)
        self.proc(live_id).stop(reason)

    def restart(self, live_id: str, reason: str = "사용자 재시작", audio_too: bool = False) -> None:
        """방송 세션은 그대로 (타이머 계속). 기본은 영상만 — 공백이 가장 짧다."""
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
        self._clear_session(live_id)
        self.store.delete(live_id)
        self._purge(live_id)

    def _purge(self, live_id: str) -> None:
        """삭제한 라이브의 찌꺼기: 실행 파일·목록·로그·지표·이벤트. (같은 ID 를 새 라이브가 다시 쓸 수 있으므로)"""
        import shutil

        run = self.paths.run_dir
        for suffix in (".liq", ".m3u", ".m3u.tmp", ".tmp", ".nowplaying.json", ".next.json", ".history.json", ".session.json", ".stunnel.conf",
                       ".stunnel.pid"):
            (run / f"{live_id}{suffix}").unlink(missing_ok=True)
        shutil.rmtree(self.paths.concat_dir / live_id, ignore_errors=True)
        for pre in ("live", "audio", "tls"):
            for f in self.paths.logs.glob(f"{pre}-{live_id}.log*"):
                f.unlink(missing_ok=True)
        lg = logging.getLogger(f"livemax.live.{live_id}")
        for hd in list(lg.handlers):
            lg.removeHandler(hd)
            hd.close()
        try:
            with db.session(self.paths.db) as c:
                c.execute("DELETE FROM metrics_live WHERE live_id=?", (live_id,))
                c.execute("DELETE FROM events WHERE live_id=?", (live_id,))
        except Exception:  # noqa: BLE001
            log.exception("라이브 기록 정리 실패 %s", live_id)
        log.info("라이브 삭제 정리 완료: %s", live_id)

    def snapshot(self) -> dict[str, dict]:
        return {lid: p.snapshot() for lid, p in list(self.procs.items())}

    def start_all(self) -> None:
        """부팅: 활성 라이브를 간격을 두고 하나씩 시작 (동시 시작 금지).

        세션이 5분 안쪽으로 끊겼으면(업데이트·컨테이너 재시작) 같은 방송으로 이어 가고 타이머도 유지한다.
        더 오래 꺼져 있었으면 유튜브 방송은 이미 끝났으므로 새 세션으로 시작한다.
        타이머가 이미 지났으면: 반복 → 새 세션, 반복 아님 → 시작하지 않는다.
        """
        gap = float(self.settings["supervisor"]["boot_start_gap_seconds"])
        first = True
        for lv in self.store.all():
            if not lv.enabled or not channels.live_key_path(self.paths, lv).exists():
                continue
            ses, now = self.session(lv.id), time.time()
            if ses.get("next_start_at"):
                continue  # 반복 대기 중 — loop 가 시각이 되면 시작
            fresh = not ses.get("started_at") or now - float(ses.get("alive_at") or 0) > 300
            end = None if fresh else lv.timer_end_at(ses.get("started_at"))
            if end and now >= end:
                if lv.timer_mode == "duration" and lv.timer_repeat:
                    fresh = True
                else:
                    self._finish_timer(lv, None)
                    continue
            if lv.timer_mode == "until" and float(lv.timer_until) <= now + 60:
                self._finish_timer(lv, None)
                continue
            if not first and self.stop_event.wait(gap):
                return
            first = False
            log.info("부팅 시작: %s%s", lv.id, " (새 방송)" if fresh else " (이어서)")
            if fresh:
                self._new_session(lv.id)
            self.proc(lv.id).start()

    def shutdown(self) -> None:
        self.stop_event.set()
        for p in list(self.procs.values()):
            if p.desired:
                p.desired = False
                p._kill_video()
                p._kill(p.audio)

    # ---------------------------------------------------------------- 타이머
    def _finish_timer(self, lv, p: LiveProc | None) -> None:
        """타이머 종료: 송출을 멈춘다. 반복이면 다음 방송 예약, 아니면 라이브를 정지 상태로."""
        repeat = lv.timer_mode == "duration" and lv.timer_repeat
        if repeat:
            nxt = time.time() + int(lv.timer_gap_minutes) * 60
            self._write_session(lv.id, next_start_at=nxt)
        else:
            self._clear_session(lv.id)
            lv.enabled = False
            try:
                self.store.save(lv)
            except ConfigError:
                log.exception("타이머 종료 저장 실패 %s", lv.id)
        p = p or self.proc(lv.id)
        msg = "타이머 종료" + (f" — {int(lv.timer_gap_minutes)}분 뒤 새 방송" if repeat else "")
        self.notify("timer", f"{lv.name}: {msg}", "유튜브 '자동 중지'가 켜져 있으면 방송이 끝나고 다시보기로 저장됩니다")
        if p.desired:
            p.stop(msg)
        else:
            p.set(state="stopped", message=msg)
            p.event("info", msg)

    def _timers(self) -> None:
        now = time.time()
        for lv in self.store.all():
            p = self.procs.get(lv.id)
            ses = self.session(lv.id)
            if p and p.desired:
                end = lv.timer_end_at(ses.get("started_at"))
                if end and now >= end:
                    self._finish_timer(lv, p)
                elif ses.get("started_at") and now - float(ses.get("alive_at") or 0) >= 60:
                    ses["alive_at"] = now
                    self._write_session(lv.id, **ses)
            elif ses.get("next_start_at") and now >= float(ses["next_start_at"]):
                if not lv.enabled or now - self.last_timer_start < 10:  # 동시 시작 금지 (11-6)
                    if not lv.enabled:
                        self._clear_session(lv.id)
                    continue
                self.last_timer_start = now
                try:
                    self.start(lv.id, new_session=True)
                    self.proc(lv.id).event("info", "타이머 반복 — 새 방송 시작")
                    self.notify("timer", f"{lv.name}: 새 방송 시작 (타이머 반복)")
                except ConfigError as e:
                    self._clear_session(lv.id)
                    self.proc(lv.id).set(state="stopped", message=str(e))

    # ---------------------------------------------------------------- 주기 작업 (감시 + 타이머 + 일일 재시작)
    def loop(self) -> None:
        while not self.stop_event.wait(5):
            now = time.time()
            wd = self.settings["watchdog"]
            for lid, p in list(self.procs.items()):
                try:
                    reason = p.watch(now)
                    if reason:
                        recent = sum(1 for t, _ in p.restarts if now - t < 3600)
                        lv_ = self.store.get(lid)
                        name = lv_.name if lv_ else lid
                        if recent >= int(wd["max_restarts_per_hour"]):
                            p.set(message=f"재시작 한도 초과 — {reason}")
                            self.notify("down", f"{name}: 재시작 한도 초과", f"1시간에 {recent}번 재시작 — {reason}. 확인이 필요합니다",
                                        dedupe_s=3600)
                            continue
                        self.notify("restart", f"{name}: 자동 재시작", reason)
                        p.restart(reason)
                    self._health(p, now)
                except Exception:  # noqa: BLE001
                    log.exception("watch 실패 %s", lid)
            try:
                self._timers()
            except Exception:  # noqa: BLE001
                log.exception("타이머 처리 실패")
            if now - self.last_m3u >= 15:
                self.last_m3u = now
                self._refresh_playlists()
            self._planned_restarts()

    def _refresh_playlists(self) -> None:
        """음악 폴더에 곡이 추가·삭제되면 송출 중인 라이브의 m3u 를 갱신 → Liquidsoap 이 재시작 없이 다시 읽음."""
        for lv in self.store.all():
            p = self.procs.get(lv.id)
            if p and p.desired:
                try:
                    if liquidsoap.write_m3u(lv, self.paths):
                        p.log.info("음악 목록 갱신")
                except Exception:  # noqa: BLE001
                    log.exception("m3u 갱신 실패 %s", lv.id)

    def _health(self, p: LiveProc, now: float) -> None:
        """송출하려는데 1분 넘게 송출이 안 되면 '중단' 알림, 다시 30초 이상 정상이면 '복구' 알림."""
        lv = self.store.get(p.id)
        name = lv.name if lv else p.id
        running = p.st.get("state") == "running" and now - float(p.st.get("progress_at") or 0) < 30
        if not p.desired:
            p.bad_since, p.down_notified = None, False
            return
        if running:
            if p.down_notified and now - float(p.st.get("ffmpeg_started_at") or now) >= 30:
                p.down_notified = False
                self.notify("recovered", f"{name}: 송출 복구", "다시 정상 송출 중입니다", dedupe_s=0)
            if not p.down_notified:
                p.bad_since = None
            return
        if p.bad_since is None:
            p.bad_since = now
        if not p.down_notified and now - p.bad_since >= 60:
            p.down_notified = True
            msg = p.st.get("message") or p.st.get("state") or ""
            self.notify("down", f"{name}: 송출 중단", f"{int(now - p.bad_since)}초째 송출 안 됨 — {msg}", dedupe_s=0)

    def _planned_restarts(self) -> None:
        now = datetime.now(timezone.utc)
        gap = 30
        for lv in self.store.all():
            p = self.procs.get(lv.id)
            if not p or not p.desired or p.st.get("state") != "running":
                continue
            end = lv.timer_end_at(self.session(lv.id).get("started_at"))
            if end and end - time.time() < 26 * 3600:
                continue  # 타이머로 곧 끝날 방송 — 재시작 공백으로 유튜브 방송이 끊기지 않게 건너뜀
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
