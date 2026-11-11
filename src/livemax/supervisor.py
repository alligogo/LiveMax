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

from . import channels, concat, config, db, ffmpeg_cmd, liquidsoap, rtmp_relay, secrets, sysmetrics, tls_relay
from .config import ConfigError, LiveStore
from .logsetup import UTCFormatter, FMT
from .paths import Paths
from .state import read_json, write_json_atomic

log = logging.getLogger("livemax.supervisor")
GROUP_ADD_GRACE_S = 20  # 묶음에 올리라고 한 뒤 이만큼 지나도 소리가 안 나오면 그 라이브만 단독 프로그램으로


class _LiveName(logging.Filter):
    """라이브 로그 줄의 [이름] 칸을 사람이 읽는 이름으로: [서버명 · 라이브명 (채널명) · live13] (DESIGN 11-D 10).
    번호는 이름을 바꾸거나 같은 이름이 생겨도 기록을 추적할 수 있게 맨 끝에 남긴다."""

    def __init__(self, label) -> None:
        super().__init__()
        self.label = label

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.name = self.label()
        except Exception:  # noqa: BLE001 — 이름을 못 구해도 로그는 남긴다
            pass
        return True
PLANNED = ("일일 재시작", "설정 적용", "사용자 재시작")  # 일부러 한 재시작 (끊김·경고로 세지 않음)
# 유튜브가 끊고 중계기가 5초 안에 다시 붙은 것 — 시청자 영향 없음. 카드에 회색으로만, 끊김·경고·재시작 한도엔 안 셈 (DESIGN 11-D 11)
QUIET_RECONNECT = "유튜브 짧은 재연결"
NOT_COUNTED = PLANNED + (QUIET_RECONNECT,)
# 밀림·재연결 알림 기준 (DESIGN 11-D 11) — '우리 문제'일 때만 눈에 띄게
SLOW_LOG_S = 10        # 밀림 요약을 로그에 남기는 기준 (최대 밀림 또는 지속 시간)
SLOW_ALERT_DUR_S = 30  # 라이브 1개가 이만큼 넘게 밀리면 알림 박스
SLOW_ALERT_DELAY_S = 10  # 또는 못 보낸 영상이 이만큼(초) 넘게 쌓였으면 알림 박스 — 짧아도 시청자가 끊김을 겪는다
                         # (실제: 18초 동안 최대 17.8초 밀리고 3초 만에 재접속한 사건이 '30초 미만·재접속 5초 이하'라 조용히 넘어갔다)
SLOW_REPEAT_N = 3      # 같은 라이브가 1시간에 이만큼 밀리면 알림 박스
QUIET_GAP_S = 5.0      # 유튜브 재연결이 이 안에 붙으면 조용히
MASS_SLOW_N = 3        # 라이브 이만큼이 같은 순간 밀리면 서버·회선 문제 → 휴대폰 알림
LIQUIDSOAP = "liquidsoap"
# 켤 때 표준 라이브러리 전체 검사를 건너뛴다: 시작 CPU 약 5.9초 → 1.0초 (4코어 서버 순간 15% → 3%),
# 쓰지 않는 옛 함수 묶음은 안 읽어 메모리 약 -9MB (DESIGN 11-C A안). 스크립트 자체 검사는 테스트(--check)에서 정식으로 한다.
LIQUIDSOAP_ARGS = ("--unsafe", "--no-deprecated")


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
        self.relay: tls_relay.Relay | rtmp_relay.RelayProc | None = None
        self.threads: list[threading.Thread] = []
        self.restarts: deque[tuple[float, str]] = deque(maxlen=50)
        self.blips: deque[tuple[float, float]] = deque(maxlen=500)  # 1초 넘는 밀림 (시각, 최대 초) — 카드의 '최근 24시간 밀림'
        self.blip_open: list | None = None   # 지금 진행 중인 1초 넘는 밀림 [시작, 최대]
        self.slow_eps: deque[float] = deque(maxlen=20)  # 2초·5초 넘는 밀림이 끝난 시각 — '1시간에 3번' 판정
        self.slow_alert_at = 0.0
        self.relay_fails = 0
        self.relay_block_until = 0.0
        self.lm_fails = 0
        self.lm_block_until = 0.0
        self.low_speed_since: float | None = None
        self.pending_reason: str | None = None
        self.intentional = False  # 마지막 ffmpeg 종료가 의도한 재시작이었나 (백오프 생략)
        self.first_run = False  # 시작 버튼(또는 부팅) 뒤 아직 한 번도 송출이 안 붙었나 — 화면에 '재시작' 대신 '시작 중'
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
            fh.addFilter(_LiveName(lambda: sup.live_label(live_id)))
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
        d["audio_pid"] = (self.sup.group_pid(self.id) if self.sup.shared_audio
                          else (self.audio.pid if self.audio and self.audio.poll() is None else None))
        rp = getattr(self.relay, "proc", None)  # 중계기(새 방식·stunnel)도 라이브 몫 CPU·메모리에 넣는다
        d["relay_pid"] = rp.pid if rp is not None and rp.poll() is None else None
        now = time.time()
        # 경고 판정용: 일부러 한 재시작(설정 적용·매일 자동·재시작 버튼)은 세지 않는다 — 진짜 문제만
        d["restarts_hour"] = sum(1 for t, r in self.restarts if now - t < 3600 and not r.startswith(NOT_COUNTED))
        ds = config.day_start(self.sup.settings, now)  # 하루 = 일일 재시작 기준 시각(기본 한국 자정)부터
        day = [m for t, m in self.blips if t >= ds]
        d["blips_day"] = {"n": len(day), "max": round(max(day), 1) if day else 0}
        d["restarts"] = [{"ts": t, "reason": r} for t, r in list(self.restarts)[-10:]]
        # 일부러 한 재시작(폴더 변경·재시작 버튼·매일 자동) 중인지 — 화면에 '영상 교체 중'으로 차분하게
        d["planned"] = bool(self.pending_reason) or (bool(self.intentional) and d.get("state") in ("starting", "restarting"))
        d["starting"] = self.first_run and d.get("state") in ("starting", "restarting")
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
            self.first_run = True
            self.set(state="starting", message="시작 중")
        self.event("info", "시작")
        if self.sup.shared_audio:
            self.sup.groups_kick()  # 음악은 채널 묶음 프로그램이 맡는다 (없으면 바로 띄움)
        for target in ((self._video_loop,) if self.sup.shared_audio else (self._audio_loop, self._video_loop)):
            t = threading.Thread(target=target, name=f"{self.id}-{target.__name__}", daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self, reason: str = "정지") -> None:
        with self.lock:
            self.desired = False
        self._kill_video()
        self._kill(self.audio)  # 묶음 모드면 self.audio 는 None — 묶음 프로그램은 다른 라이브가 쓰므로 건드리지 않는다
        if self.sup.shared_audio:
            self.sup.groups_kick()  # 그 채널에 송출 중인 라이브가 하나도 없으면 묶음 프로그램도 끈다
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
        if audio_too and not self.sup.shared_audio:
            self.stop(reason)
            self.start()
            return
        if audio_too:
            self.sup.groups_kick()  # 묶음 모드: 재생 방식·크로스페이드가 바뀌면 이 라이브 음악만 새 주소로 다시 올린다 (다른 라이브는 그대로)
        self.pending_reason = reason
        self.set(state="restarting", message=reason)
        self._kill_video(keep_relay=True)

    @staticmethod
    def _kill(p: subprocess.Popen | None, sig=signal.SIGTERM, wait: float = 8) -> None:
        if p and p.poll() is None:
            p.send_signal(sig)
            try:
                p.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                p.kill()

    def _kill_video(self, keep_relay: bool = False) -> None:
        """ffmpeg 정지. keep_relay: 새 중계기는 살려 둔다 (영상만 다시 띄울 때 — 유튜브 연결 유지)."""
        self._kill(self.video, signal.SIGINT)
        if self.relay and not (keep_relay and isinstance(self.relay, rtmp_relay.RelayProc)):
            self.relay.stop()
            self.relay = None

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
            self.audio = subprocess.Popen([LIQUIDSOAP, *LIQUIDSOAP_ARGS, str(liq)], stdin=subprocess.DEVNULL,
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

            # 오디오 준비 대기. 묶음 모드: 묶음 프로그램이 이 라이브 음악을 (지금 설정으로) 올려 소리가 나올 때까지
            deadline = time.time() + float(settings["output"]["audio_connect_timeout"])
            if self.sup.shared_audio:
                audio_url = self.sup.audio_url(self.id)
                while audio_url is None and time.time() < deadline and self.desired:
                    time.sleep(0.5)
                    audio_url = self.sup.audio_url(self.id)
            else:
                while time.time() < deadline and self.desired and not _port_open(live.harbor_port):
                    time.sleep(0.5)
                audio_url = f"http://127.0.0.1:{live.harbor_port}/{live.id}.aac" if _port_open(live.harbor_port) else None
            if audio_url is None:
                self.set(state="error", message="오디오 준비 안 됨 (음악 폴더 확인)")
                return False

            base = (live.output_url or settings["output"]["default_url"]).rstrip("/")
            rcfg = settings["output"]["tls_relay"]
            out = None
            # 새 중계기(lmrelay): 라이브별로 켠 경우만. 못 띄우거나 연속 실패하면 기존 방식(stunnel)으로
            use_lm = (live.fast_relay and settings["output"].get("lm_relay", {}).get("enabled", True)
                      and time.time() >= self.lm_block_until)
            sig = (base, key)
            if isinstance(self.relay, rtmp_relay.RelayProc) and not (use_lm and self.relay.alive() and self.relay.sig == sig):
                self.relay.stop()  # 방식·주소·키가 바뀌었거나 죽었으면 새로
                self.relay = None
            if use_lm:
                if isinstance(self.relay, rtmp_relay.RelayProc):
                    lport = self.relay.port  # 살아 있는 중계기 재사용 → 유튜브 연결 그대로 (영상 교체·일일 재시작)
                else:
                    lm = rtmp_relay.RelayProc(live.id, paths.logs, on_event=self._relay_event)
                    lport = lm.start(base, key, verify=bool(rcfg.get("verify", True)), ipv4=bool(rcfg.get("ipv4", True)))
                    if lport:
                        lm.sig = sig
                        self.relay = lm
                if lport:
                    out = tls_relay.OutputPlan(url=f"tcp://127.0.0.1:{lport}", extra_args=["-flush_packets", "0"],
                                               mode="lm-relay")
            if out is None:
                relay_port = None
                if rcfg.get("enabled", True) and time.time() >= self.relay_block_until:
                    self.relay = tls_relay.Relay(live.id, paths.run_dir, paths.logs)
                    relay_port = self.relay.start(base, verify=bool(rcfg.get("verify", True)),
                                                  ipv4=bool(rcfg.get("ipv4", True)))
                out = tls_relay.plan(base, key, relay_port)
            args = ffmpeg_cmd.build(list_path, audio_url, out.url,
                                    extra_out=out.extra_args)
            self.log.info("ffmpeg 시작 (%s, %s): %s", out.mode, list_path.name, ffmpeg_cmd.redacted(args, key))
            self.video = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          stdin=subprocess.DEVNULL)
            started = time.time()
            self.low_speed_since = None
            self.first_run = False
            self.set(state="running", message="", transport=out.mode, ffmpeg_started_at=started,
                     progress_at=started)
            threading.Thread(target=self._read_stderr, args=(self.video,), daemon=True).start()
            self._read_progress(self.video)
            rc = self.video.wait()
            ran = time.time() - started
            if self.relay and not (isinstance(self.relay, rtmp_relay.RelayProc) and self.desired and self.relay.alive()):
                self.relay.stop()  # 새 중계기는 라이브가 켜져 있는 동안 살려 둔다 (다음 ffmpeg 가 같은 유튜브 연결로)
                self.relay = None
            reason, self.pending_reason = self.pending_reason, None
            self.intentional = bool(reason)
            if out.mode == "rtmps-relay" and ran < 60 and not reason and self.desired:
                self.relay_fails += 1
                if self.relay_fails >= 3:
                    self.relay_block_until = time.time() + 1800
                    self.relay_fails = 0
                    self.event("warn", "TLS 중계 연속 실패 — 30분간 직접 RTMPS 로 송출")
            elif out.mode == "lm-relay" and ran < 60 and not reason and self.desired:
                self.lm_fails += 1
                if self.lm_fails >= 3:
                    self.lm_block_until = time.time() + 1800
                    self.lm_fails = 0
                    self.event("warn", "새 중계기 연속 실패 — 30분간 기존 방식(stunnel)으로 송출")
            elif ran >= 60:
                self.relay_fails = 0
                self.lm_fails = 0
            if self.desired and not reason:
                self.restarts.append((time.time(), f"송출 끊김 rc={rc}"))
                self.event("error", f"ffmpeg 종료 rc={rc} (가동 {int(ran)}초) — 재기동")
                self.sup.notify("restart", f"{live.name}: 송출 프로그램 종료 — 자동 재시작", f"ffmpeg rc={rc}, 가동 {int(ran)}초")
                self.set(state="restarting", message=f"ffmpeg 종료 rc={rc}")
            return ran >= 300
        finally:
            conn.close()

    def _relay_event(self, ev: dict) -> None:
        """새 중계기가 유튜브에 혼자 다시 붙었을 때 — 송출 프로그램은 안 멈췄지만 끊김으로 기록한다 (카드·관제·로그)."""
        kind = ev.get("event")
        if kind == "stats":  # 15초마다: 유튜브로 실제 나간 양·밀린 시간 (DB 기록용, 로그 없음)
            self.set(kbps_in=ev.get("kbps_in"), kbps_out=ev.get("kbps_out"), delay_s=ev.get("delay"), relay_at=time.time())
            self._track_blip(float(ev.get("delay") or 0))
            return
        if kind == "slow_start":  # 2초 넘게 5초 이상 밀림 — 로그는 끝날 때 한 번만, 동시 밀림 판정에만 쓴다
            self.sup.note_slow(self.id, True)
            return
        if kind == "no_audio":  # 영상만 가고 소리가 없다 — ffmpeg 가 음악 연결을 잃음 (스스로 다시 붙기에 실패한 뒤)
            self._auto_restart("소리 없음", f"영상만 송출되고 소리가 {ev.get('sec')}초째 없음 — 송출 프로그램을 다시 띄워 음악을 다시 붙임")
            return
        if kind == "audio_back":
            self.log.info("소리가 다시 나감 (%s초 만에)", ev.get("sec"))
            return
        if kind == "no_input":
            sec = float(ev.get("sec") or 0)
            if sec >= 30:
                self._auto_restart("입력 없음", f"송출 프로그램이 {sec:.0f}초째 아무것도 안 보냄 — 다시 띄움")
            else:
                self.event("warn", f"송출 프로그램이 {sec:.0f}초째 아무것도 안 보냄 (음악 프로그램이 다시 켜지는 중이면 곧 이어짐)")
            return
        if kind == "input_back":
            self.event("warn", f"송출 프로그램이 다시 보내기 시작 — {ev.get('sec')}초 동안 유튜브로 영상·소리가 안 나감")
            return
        if kind == "stall":
            msg = f"중계기가 {ev.get('sec')}초 멈춤 — 그동안 유튜브로 못 보냄 (서버 처리 지연, 회선 아님)"
            if float(ev.get("sec") or 0) >= 3:
                self.event("warn", msg)
            else:
                self.log.warning(msg)
            return
        if kind == "slow_end":
            self.sup.note_slow(self.id, False)
            now = time.time()
            dur, mx = float(ev.get("dur") or 0), float(ev.get("max_delay") or 0)
            where = {"network": "서버→유튜브 회선", "server": "서버 처리 지연"}.get(ev.get("where"), "")
            msg = (f"유튜브 전송 밀림 {ev.get('dur')}초 — 최대 {ev.get('max_delay')}초 밀림, "
                   f"유튜브로 {ev.get('kbps_out')}kbps (보낼 양 {ev.get('kbps_in')}kbps)"
                   + (f", 막힌 곳: {where}" if where else "") + ". 연결은 유지됨")
            self.slow_eps.append(now)
            recent = sum(1 for t in self.slow_eps if now - t < 3600)
            if dur >= SLOW_ALERT_DUR_S or mx >= SLOW_ALERT_DELAY_S:
                self.event("warn", msg)
            elif recent >= SLOW_REPEAT_N and now - self.slow_alert_at > 3600:
                self.slow_alert_at = now
                self.event("warn", f"1시간에 밀림 {recent}번 — 이 라이브의 파일·설정을 확인해 보세요 (마지막: {msg})")
            elif dur >= SLOW_LOG_S or mx >= SLOW_LOG_S:  # 짧은 밀림은 숫자 기록(DB)과 카드 '최근 24시간 밀림'에만
                self.log.warning(msg + f" (커널 대기 {ev.get('kern_kb')}KB · 중계기 {ev.get('app_kb')}KB · 멈칫 {ev.get('lag')}초)")
            return
        if kind == "reconnect":
            self.sup.note_slow(self.id, False)
            gap = float(ev.get("gap") or 0)
            if gap <= QUIET_GAP_S:  # 유튜브 정비 등으로 끊겼다가 바로 붙음 — 시청자 영향 없음
                self.restarts.append((time.time(), f"{QUIET_RECONNECT} {gap:.1f}초"))
                self.log.info("유튜브가 연결을 끊음 → 중계기가 %.1f초 만에 다시 접속 (송출 프로그램은 계속)", gap)
                return
            self.restarts.append((time.time(), f"유튜브 재접속 {gap:.1f}초"))
            self.event("warn", f"유튜브가 연결을 끊음 → 중계기가 {gap:.1f}초 만에 다시 접속 (송출 프로그램은 계속)")

    def _auto_restart(self, reason: str, detail: str) -> None:
        """중계기가 내용 이상(소리 없음·입력 없음)을 알렸을 때 — 그 라이브의 ffmpeg 만 다시 띄운다 (유튜브 연결은 유지).
        1시간 재시작 한도를 넘으면 더 건드리지 않고 알림만 (감시 루프와 같은 규칙)."""
        if not self.desired or self.st.get("state") != "running":
            return
        now = time.time()
        lv = self.sup.store.get(self.id)
        name = lv.name if lv else self.id
        recent = sum(1 for t, r in self.restarts if now - t < 3600 and not r.startswith(NOT_COUNTED))
        if recent >= int(self.sup.settings["watchdog"]["max_restarts_per_hour"]):
            self.set(message=f"재시작 한도 초과 — {reason}")
            self.event("error", f"{detail} (재시작 한도 초과 — 자동 재시작 안 함)")
            self.sup.notify("down", f"{name}: 재시작 한도 초과", f"1시간에 {recent}번 재시작 — {reason}. 확인이 필요합니다", dedupe_s=3600)
            return
        self.event("warn", detail)
        self.sup.notify("restart", f"{name}: 자동 재시작", detail)
        self.restart(reason)

    def _track_blip(self, delay: float) -> None:
        """15초 숫자로 '1초 넘는 밀림' 횟수·최대를 센다 (카드에 회색으로, 알림 없음)."""
        if delay >= 1.0:
            if self.blip_open is None:
                self.blip_open = [time.time(), delay]
            else:
                self.blip_open[1] = max(self.blip_open[1], delay)
        elif self.blip_open is not None:
            self.blips.append((self.blip_open[0], self.blip_open[1]))
            self.blip_open = None

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
            # 1분마다 남기던 정상 진행 기록은 뺐다 — 느려질 때만 남긴다 (감시는 웹·알림이 한다)
            # 시작 직후 첫 측정(speed=0)은 가짜 경고라 10초 지나서부터 본다
            if speed is not None and speed < 0.97 and (ot or 0) >= 10 and time.time() - last_log >= 60:
                last_log = time.time()
                self.log.warning("송출 느림 speed=%s bitrate=%s", speed, br)
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
        self._labels: dict[str, tuple[float, str]] = {}
        self._slow_now: dict[str, float] = {}  # 지금 밀리는 중인 라이브 → 시작 시각
        # 음악 프로그램 채널별 묶음 (B안). 켜고 끄기는 LiveMax 가 다시 켜질 때(업데이트·재시작) 적용 — 도중에 바꾸면 꼬인다
        self.shared_audio = bool(settings.get("audio_shared", True))
        # 묶음 키 → {proc, port, seq, inst{라이브 id → {token, sig, since, ready}}, fails, next_try, started}
        self.groups: dict[str, dict] = {}
        self._glock = threading.Lock()
        self._alock = threading.Lock()
        self._gmap: dict[str, str] = {}  # 라이브 id → 묶음 이름 (한 번 정하면 LiveMax 가 다시 켜질 때까지 그대로)
        self._gch: dict[str, str] = {}   # 라이브 id → 자리를 잡을 때의 채널 (채널이 바뀌면 자리를 다시 잡는다)
        self._mass_slow_at = 0.0
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.done_today: dict[str, str] = {}
        self.last_planned = 0.0
        self.last_timer_start = 0.0
        self.boot_pending: set[str] = set()  # 부팅 줄에서 차례를 기다리는, 이어 갈 라이브 (alive_at 유지용)
        self.last_m3u = 0.0
        self.notify = lambda *a, **k: False  # daemon 이 Notifier.send 로 바꿔 끼운다

    def note_slow(self, live_id: str, on: bool) -> None:
        """라이브 여러 개가 같은 순간 밀리면 서버·회선(우리) 문제 → 휴대폰 알림 (DESIGN 11-D 11).
        라이브 하나만 밀리는 건 유튜브 쪽 출렁임이라 알리지 않는다. 막 켜진(1분 안) 라이브는 세지 않음."""
        now = time.time()
        with self.lock:
            if not on:
                self._slow_now.pop(live_id, None)
                return
            p = self.procs.get(live_id)
            if p is not None and now - float(p.st.get("ffmpeg_started_at") or 0) < 60:
                return
            self._slow_now[live_id] = now
            for lid in [k for k, t in self._slow_now.items() if now - t > 600]:  # 끝 소식이 안 온 오래된 것
                self._slow_now.pop(lid, None)
            n = len(self._slow_now)
            if n < MASS_SLOW_N or now - self._mass_slow_at < 600:
                return
            self._mass_slow_at = now
            names = ", ".join(sorted(self.live_label(k).split(" · ")[-2] if " · " in self.live_label(k) else k for k in self._slow_now))
        msg = f"라이브 {n}개가 동시에 유튜브 전송 밀림 — 서버나 회선 문제일 수 있습니다 ({names})"
        log.warning(msg)
        try:
            with db.session(self.paths.db) as c:
                db.add_event(c, "error", "supervisor", msg)
        except Exception:  # noqa: BLE001
            pass
        self.notify("down", f"동시 밀림 {n}개", msg, dedupe_s=600)

    def live_label(self, live_id: str) -> str:
        """로그·알림에 쓰는 이름: '서버명 · 라이브명 (채널명) · live13'. 1분 동안 기억 (로그 줄마다 파일을 읽지 않게)."""
        now = time.monotonic()
        hit = self._labels.get(live_id)
        if hit and now - hit[0] < 60:
            return hit[1]
        lv = self.store.get(live_id)
        name = lv.name if lv else live_id
        ch = None
        if lv and lv.channel_id:
            try:
                ch = channels.ChannelStore(self.paths).get(lv.channel_id)
            except Exception:  # noqa: BLE001
                ch = None
        label = " · ".join(x for x in (config.server_name(self.settings), f"{name} ({ch.name})" if ch else name, live_id) if x)
        self._labels[live_id] = (now, label)
        return label

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

    def set_elapsed(self, live_id: str, seconds: float) -> float:
        """화면에서 방송 시간을 직접 고침 (송출은 그대로, 숫자만). 송출 중(켜 둔) 라이브만. 새 시작 시각을 돌려준다.
        방송 시간 타이머도 이 시작 시각을 기준으로 하므로 종료 예정도 같이 움직인다."""
        p = self.procs.get(live_id)
        if not (p and p.desired):
            raise ConfigError("송출 중인 라이브만 방송 시간을 바꿀 수 있습니다")
        seconds = float(seconds)
        if not 0 <= seconds <= 3650 * 86400:
            raise ConfigError("방송 시간 범위 오류")
        lv = self.store.get(live_id)
        if lv and lv.timer_mode == "duration" and seconds >= int(lv.timer_minutes) * 60:
            # 타이머 길이 이상으로 두면 저장하자마자 타이머 종료로 송출이 멈춘다 (v2.17)
            h, m = divmod(int(lv.timer_minutes), 60)
            raise ConfigError(f"타이머({h}시간 {m}분)보다 길게 할 수 없습니다 — 라이브 설정에서 타이머를 먼저 늘리세요")
        now = time.time()
        ses = self.session(live_id)
        ses["started_at"], ses["alive_at"] = now - seconds, now
        self._write_session(live_id, **ses)
        return ses["started_at"]

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
        for suffix in (".liq", ".m3u", ".m3u.tmp", ".tmp", ".nowplaying.json", ".next.json", ".session.json", ".stunnel.conf",
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

        방송 시간 기준 (v2.16, 사용자 결정 — DESIGN 11-G): **사용자가 정지하지 않은 라이브는 서버가 얼마나 꺼져 있었든
        같은 방송으로 이어 간다** (업데이트·재부팅·장애 모두). 방송 시간이 0이 되는 건 사용자 정지·삭제·타이머 종료뿐.
        유튜브 쪽 방송은 사용자가 직접 열고 닫으므로, 어긋나면 화면에서 시간을 직접 고친다 (set_elapsed).
        (v2.13 까지 '5분 넘게 꺼졌으면 새 방송' 규칙 → 업데이트 때 뒤쪽 라이브가 리셋되던 원인 · v2.14 에서 부팅 시작 기준으로 · v2.16 에서 삭제)
        타이머가 이미 지났으면: 반복 → 새 세션, 반복 아님 → 시작하지 않는다.
        """
        gap = float(self.settings["supervisor"]["boot_start_gap_seconds"])
        boot_at = time.time()
        plan = []
        for lv in self.store.all():
            if not lv.enabled or not channels.live_key_path(self.paths, lv).exists():
                continue
            ses = self.session(lv.id)
            if ses.get("next_start_at"):
                continue  # 반복 대기 중 — loop 가 시각이 되면 시작
            fresh = not ses.get("started_at")
            if not fresh:
                ses["alive_at"] = boot_at
                self._write_session(lv.id, **ses)
            plan.append((lv, fresh))
        self.boot_pending = {lv.id for lv, fresh in plan if not fresh}
        first = True
        for lv, fresh in plan:
            self.boot_pending.discard(lv.id)
            lv = self.store.get(lv.id)
            if not lv or not lv.enabled:
                continue  # 줄 서 있는 동안 사용자가 정지·삭제함
            ses, now = self.session(lv.id), time.time()
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
            io0, t0 = sysmetrics.read_swap_io(), time.monotonic()
            if not first and self.stop_event.wait(gap):
                return
            if not first and self._wait_for_memory(io0, time.monotonic() - t0):
                return
            first = False
            log.info("부팅 시작: %s%s", lv.id, " (새 방송)" if fresh else " (이어서)")
            if fresh:
                self._new_session(lv.id)
            self.proc(lv.id).start()

    def _wait_for_memory(self, io0: int, span: float, max_extra: float = 30.0) -> bool:
        """업데이트·부팅 때 40개가 10초 간격으로 줄줄이 켜지면 메모리가 몰린다 (v2.10).
        다음 라이브를 켜기 전에, 남은 메모리가 '남길 여유 + 300MB' 보다 적거나 방금 간격(span 초) 동안 스왑을 계속
        오갔으면(초당 200페이지 이상) 5초씩 최대 max_extra 초까지 더 기다린다. 여유가 있으면 바로 넘어간다 (복구 시간 그대로).
        stop 이 걸리면 True."""
        reserve = float(self.settings.get("capacity", {}).get("mem_reserve_mb", 700)) + 300
        waited = 0.0
        while True:
            used, total = sysmetrics.read_mem()
            io1 = sysmetrics.read_swap_io()
            busy_swap = (io1 - io0) / max(span, 1.0) >= 200
            if ((total - used) >= reserve and not busy_swap) or waited >= max_extra:
                return False
            if waited == 0:
                log.info("메모리가 빠듯해 다음 라이브 시작을 잠시 미룸 (남은 %.0fMB%s)", total - used, ", 스왑 활동" if busy_swap else "")
            io0, span = io1, 5.0
            if self.stop_event.wait(5):
                return True
            waited += 5

    def shutdown(self) -> None:
        self.stop_event.set()
        for p in list(self.procs.values()):
            if p.desired:
                p.desired = False
                p._kill_video()
                p._kill(p.audio)
        for g in list(self.groups.values()):
            LiveProc._kill(g.get("proc"))

    # ---------------------------------------------------------------- 음악 묶음 (무중단, DESIGN 11-F)
    def group_max(self) -> int:
        try:
            return max(1, int(self.settings.get("audio_group_max") or liquidsoap.GROUP_MAX_DEFAULT))
        except (TypeError, ValueError):
            return liquidsoap.GROUP_MAX_DEFAULT

    def _assign(self, lives: list) -> dict[str, str]:
        """라이브 → 묶음. 한 번 정해진 자리는 LiveMax 가 다시 켜질 때까지 그대로 (다른 라이브가 생기거나 지워져도 안 옮긴다 —
        옮기면 그 라이브 음악이 끊긴다). 새 라이브만 그 채널에서 자리가 남은 묶음(없으면 새 묶음)에 넣는다."""
        from .catalog import natural_key

        ids = {lv.id: lv for lv in lives}
        for lid in list(self._gmap):
            lv = ids.get(lid)
            if lv is None or self._gch.get(lid) != (lv.channel_id or ""):
                self._gmap.pop(lid, None)
                self._gch.pop(lid, None)
        counts: dict[str, int] = {}
        for k in self._gmap.values():
            counts[k] = counts.get(k, 0) + 1
        mx = self.group_max()
        for lv in sorted(lives, key=lambda x: natural_key(x.id)):
            if lv.id in self._gmap:
                continue
            if not lv.channel_id:
                key = f"solo-{lv.id}"
            else:
                n = 1
                while True:
                    key = f"ch-{lv.channel_id}" if n == 1 else f"ch-{lv.channel_id}-{n}"
                    if counts.get(key, 0) < mx:
                        break
                    n += 1
            self._gmap[lv.id] = key
            self._gch[lv.id] = lv.channel_id or ""
            counts[key] = counts.get(key, 0) + 1
        return self._gmap

    def _group_of(self, lv) -> str:
        """그 라이브가 들어간 묶음 이름 (아직 자리를 안 잡았으면 지금 잡는다)."""
        k = self._gmap.get(lv.id)
        if k is None:
            with self._alock:
                k = self._assign([x for x in self.store.all() if x.audio_folders]).get(lv.id) or liquidsoap.group_key(lv)
        return k

    def group_pid(self, live_id: str) -> int | None:
        g = self.groups.get(self._gmap.get(live_id, ""))
        pr = g and g.get("proc")
        return pr.pid if pr and pr.poll() is None else None

    def audio_log_name(self, live_id: str) -> str:
        """사이트 로그 창의 '음악' 탭이 읽을 파일 이름 (묶음 모드면 채널 묶음 로그)."""
        lv = self.store.get(live_id)
        return liquidsoap.group_log_name(self._group_of(lv)) if (self.shared_audio and lv) else f"audio-{live_id}"

    def group_members(self, live_id: str) -> list[str]:
        """같은 음악 묶음에 들어 있는 라이브 id 들 (묶음 모드가 아니면 자기 하나)."""
        lv = self.store.get(live_id)
        if not (self.shared_audio and lv):
            return [live_id]
        k = self._group_of(lv)
        return [i for i, g in list(self._gmap.items()) if g == k] or [live_id]

    @staticmethod
    def _mount_ok(port: int, mount: str) -> bool:
        """그 음악 주소가 실제로 소리를 내보내는지 (포트만 열려 있고 주소는 아직 없을 수 있다 — 묶음 포트는 여럿이 같이 씀)."""
        import http.client

        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
            try:
                c.request("GET", "/" + mount)
                return c.getresponse().status == 200
            finally:
                c.close()
        except (OSError, http.client.HTTPException):
            return False

    def audio_url(self, live_id: str) -> str | None:
        """묶음 모드: 그 라이브의 음악 주소 — 지금 설정으로 올라와 소리가 나올 때만. 아니면 None (ffmpeg 는 기다린다)."""
        lv = self.store.get(live_id)
        g = self.groups.get(self._gmap.get(live_id, ""))
        if not lv or not g:
            return None
        inst = g["inst"].get(live_id)
        pr = g.get("proc")
        if not inst or inst["sig"] != liquidsoap.audio_sig(lv) or pr is None or pr.poll() is not None:
            return None
        mount = f"{inst['token']}.aac"
        if not inst["ready"]:
            if not self._mount_ok(g["port"], mount):
                return None
            inst["ready"] = True
        return f"http://127.0.0.1:{g['port']}/{mount}"

    def _group_wait(self, key: str, proc: subprocess.Popen) -> None:
        """묶음 프로그램이 죽으면 5초 주기를 기다리지 않고 바로 다시 띄운다 — ffmpeg 가 다시 붙기를 시도하는 동안(약 26초) 안에
        돌아오게. 일부러 끈 경우(송출 중인 라이브 없음)는 groups_tick 이 아무것도 하지 않는다."""
        proc.wait()
        if self.stop_event.is_set():
            return
        self.groups_tick()  # 죽음을 기록하고 다시 띄울 시각을 정한다
        g = self.groups.get(key)
        if g and g.get("proc") is None:
            self.stop_event.wait(max(0.0, g["next_try"] - time.time()) + 0.05)
            if not self.stop_event.is_set():
                self.groups_tick()

    def groups_kick(self) -> None:
        """라이브 시작·정지·설정 변경 직후 바로 한 번 맞춘다 (5초 주기를 기다리지 않게)."""
        threading.Thread(target=self.groups_tick, name="groups-kick", daemon=True).start()

    def groups_tick(self) -> None:
        """묶음마다: 송출 중인 라이브가 하나라도 있으면 묶음 프로그램을 띄우고, 없으면 끈다.
        돌고 있는 프로그램은 다시 켜지 않는다 — 라이브 추가·삭제·음악 설정 변경은 members.json 만 바꾸면 프로그램이
        그 라이브 몫만 올리고 내린다 (v2.12 까지는 통째로 다시 켜서 같은 묶음의 다른 라이브 소리가 끊겼다)."""
        if not self._glock.acquire(blocking=False):
            return
        try:
            now = time.time()
            withaudio = [lv for lv in self.store.all() if lv.audio_folders]
            with self._alock:
                gmap = dict(self._assign(withaudio))
            members: dict[str, list] = {}
            for lv in withaudio:
                members.setdefault(gmap[lv.id], []).append(lv)
            for key in list(self.groups):
                if key not in members:
                    LiveProc._kill(self.groups.pop(key).get("proc"))
                    liquidsoap.members_path(self.paths, key).unlink(missing_ok=True)
            backoff = self.settings["supervisor"]["restart_backoff_seconds"]
            for key, lives in members.items():
                g = self.groups.setdefault(key, {"proc": None, "port": None, "seq": 0, "inst": {}, "fails": 0,
                                                 "next_try": 0.0, "started": 0.0})
                inst: dict[str, dict] = g["inst"]
                on_air = [lv for lv in lives if (self.procs.get(lv.id) and self.procs[lv.id].desired)]
                pr = g["proc"]
                if pr is not None and pr.poll() is not None:  # 죽음 → 같은 포트·같은 주소로 다시 (ffmpeg 가 스스로 다시 붙는다)
                    rc = pr.returncode
                    g["fails"] = 0 if now - g["started"] > 120 else g["fails"] + 1
                    g["next_try"] = now + float(backoff[min(g["fails"], len(backoff) - 1)])
                    g["proc"] = pr = None
                    if g["fails"] >= 3:
                        g["port"] = None  # 계속 못 뜨면 포트를 바꿔 본다 (그 묶음 ffmpeg 는 무음 감지로 다시 붙음)
                    if on_air:
                        log.error("음악 묶음 %s 종료 rc=%s — 다시 띄움 (송출 중 %s)", key, rc, ", ".join(lv.id for lv in on_air))
                        for lv in on_air:
                            self.procs[lv.id].event("error", f"오디오(Liquidsoap 묶음 {key}) 종료 rc={rc} — 재기동")
                if not on_air:  # 그 묶음에 송출 중인 라이브가 없음 → 프로그램도 끈다 (내린 라이브 찌꺼기도 이때 사라진다)
                    if pr is not None:
                        LiveProc._kill(pr)
                        g["proc"] = None
                    if inst:
                        inst.clear()
                        liquidsoap.write_members(self.paths, key, [])
                    continue
                # 항목 맞추기: 없어진 라이브·설정이 바뀐 라이브는 내리고, 송출하려는 라이브는 (새 이름으로) 올린다.
                # 정지한 라이브의 항목은 그대로 둔다 — 다시 시작하면 바로 붙고, 내려도 찌꺼기가 남아 이득이 없다
                ids = {lv.id for lv in lives}
                for lid in [x for x in inst if x not in ids]:
                    del inst[lid]
                for lv in lives:
                    sig = liquidsoap.audio_sig(lv)
                    cur = inst.get(lv.id)
                    if cur is not None and cur["sig"] != sig:
                        del inst[lv.id]
                        cur = None
                    if cur is None and lv in on_air:
                        g["seq"] += 1
                        inst[lv.id] = {"token": f"{lv.id}_{g['seq']}", "sig": sig, "since": now, "ready": False}
                        try:
                            liquidsoap.write_m3u(lv, self.paths)
                        except Exception:  # noqa: BLE001
                            log.exception("m3u 쓰기 실패 %s", lv.id)
                if g["port"] is None:
                    g["port"] = tls_relay.free_port()
                entries = []
                for lv in lives:
                    if lv.id in inst:
                        try:
                            entries.append(liquidsoap.member_entry(lv, self.paths, inst[lv.id]["token"], g["port"]))
                        except ValueError as e:
                            log.error("음악 묶음 %s: %s 를 넣을 수 없음 — %s", key, lv.id, e)
                if liquidsoap.write_members(self.paths, key, entries) and pr is not None:
                    log.info("음악 묶음 %s 구성 변경 — 다시 켜지 않고 반영 (%s)", key, ", ".join(e["id"] for e in entries))
                if pr is None:
                    if now < g["next_try"]:
                        continue
                    path = liquidsoap.write_dynamic(self.paths, self.settings, key)
                    g["proc"] = subprocess.Popen([LIQUIDSOAP, *LIQUIDSOAP_ARGS, str(path)], stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    g["started"] = now
                    for i in inst.values():
                        i["since"], i["ready"] = now, False
                    threading.Thread(target=self._group_wait, args=(key, g["proc"]), name=f"group-wait-{key}", daemon=True).start()
                    log.info("음악 묶음 %s 시작 (포트 %d, 라이브 %d개: %s)", key, g["port"], len(entries),
                             ", ".join(e["live"] for e in entries))
                    continue
                # 보험: 올리라고 한 지 한참인데 소리가 안 나오는 라이브는 단독 프로그램으로 뺀다 (기존 묶음은 그대로)
                if now - g["started"] > GROUP_ADD_GRACE_S and not key.startswith("solo-"):
                    for lv in on_air:
                        i = inst.get(lv.id)
                        if i and not i["ready"] and now - i["since"] > GROUP_ADD_GRACE_S:
                            if self._mount_ok(g["port"], f"{i['token']}.aac"):
                                i["ready"] = True
                                continue
                            with self._alock:
                                self._gmap[lv.id] = f"solo-{lv.id}"
                            del inst[lv.id]
                            self.procs[lv.id].event("error", f"음악 묶음 {key} 에 올리지 못함 — 이 라이브만 단독 음악 프로그램으로 전환")
        finally:
            self._glock.release()

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
            elif lv.id in self.boot_pending and ses.get("started_at") and now - float(ses.get("alive_at") or 0) >= 60:
                ses["alive_at"] = now  # 부팅 줄에서 차례를 기다리는 중 — 유튜브 방송은 살아 있는 것으로 본다
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
            if self.shared_audio:
                try:
                    self.groups_tick()
                except Exception:  # noqa: BLE001
                    log.exception("음악 묶음 관리 실패")
            wd = self.settings["watchdog"]
            for lid, p in list(self.procs.items()):
                try:
                    reason = p.watch(now)
                    if reason:
                        recent = sum(1 for t, r in p.restarts if now - t < 3600 and not r.startswith(NOT_COUNTED))
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
