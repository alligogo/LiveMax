"""라이브 1개의 영상 송출 프로세스 — live@<id>.service

    1) 지금부터 24시간치 concat 목록 생성 (시간대 편성 반영)
    2) Liquidsoap harbor(오디오)가 열릴 때까지 대기
    3) ffmpeg -c:v copy -c:a copy 로 먹싱 → RTMPS
    4) ffmpeg -progress 출력을 파싱해 speed / out_time / 현재 클립을 상태 파일에 기록
       (/run/livemax/<id>.status.json — 워치독·수집기·대시보드가 읽음)

종료되면 ffmpeg 의 종료 코드로 끝난다. systemd Restart=always 가 2초 뒤 다시 띄우고,
재사용 스트림 키라면 유튜브는 같은 방송으로 복귀한다 (DESIGN 6.2).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import concat, config, db, ffmpeg_cmd, logsetup, secrets
from .state import write_json_atomic

EX_CONFIG = 78  # systemd RestartPreventExitStatus=78 — 비활성 라이브는 재시작하지 않음


def wait_for_port(port: int, timeout: float, log: logging.Logger) -> bool:
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                log.info("오디오 harbor 연결 확인 (port %d, %d회째)", port, attempt)
                return True
        except OSError:
            time.sleep(1)
    return False


def _parse_out_time(v: str) -> float | None:
    # out_time=00:12:34.567890
    try:
        h, m, s = v.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None


class Runner:
    def __init__(self, live_id: str):
        self.server = config.load_server()
        self.paths = config.get_paths(self.server)
        self.log = logsetup.setup(f"live-{live_id}", self.server)
        self.live_id = live_id
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        self.status_path = self.paths.status_file(live_id)
        self.status: dict = {"live_id": live_id, "state": "starting", "runner_pid": os.getpid(),
                             "started_at": time.time(), "updated_at": time.time()}
        self.meta_path: Path | None = None

    # ---------------- 상태 ----------------
    def publish(self, **kw) -> None:
        self.status.update(kw)
        self.status["updated_at"] = time.time()
        try:
            write_json_atomic(self.status_path, self.status)
        except OSError as e:
            self.log.error("상태 파일 쓰기 실패: %s", e)

    # ---------------- 진행 ----------------
    def _read_progress(self) -> None:
        assert self.proc and self.proc.stdout
        block: dict[str, str] = {}
        last_log = 0.0
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            block[k] = v
            if k != "progress":
                continue
            speed = None
            sp = block.get("speed", "").rstrip("x").strip()
            try:
                speed = float(sp) if sp and sp != "N/A" else None
            except ValueError:
                pass
            out_t = _parse_out_time(block.get("out_time", "")) if block.get("out_time") else None
            clip = concat.current_clip(self.meta_path, out_t) if (self.meta_path and out_t is not None) else None
            self.publish(
                state="running",
                speed=speed,
                out_time_s=out_t,
                frame=block.get("frame"),
                fps=block.get("fps"),
                bitrate=block.get("bitrate"),
                total_size=block.get("total_size"),
                drop_frames=block.get("drop_frames"),
                dup_frames=block.get("dup_frames"),
                current_clip=clip,
                progress=v,
            )
            now = time.time()
            if now - last_log >= 60:  # 1분마다 진행 상황을 로그에 (상세 로그 원칙)
                last_log = now
                self.log.info("progress speed=%s out_time=%s frame=%s bitrate=%s clip=%s",
                              speed, block.get("out_time"), block.get("frame"), block.get("bitrate"),
                              (clip or {}).get("path"))
            # 시작 직후 몇 초는 speed 가 0 에 가깝게 나오므로 경고하지 않는다
            if speed is not None and (out_t or 0) > 10 and speed < float(self.server["watchdog"]["speed_min"]):
                self.log.warning("speed 저하: %.3fx", speed)
            block = {}

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        flog = logging.getLogger(f"livemax.ffmpeg.{self.live_id}")
        for raw in self.proc.stderr:
            line = secrets.redact(raw.decode("utf-8", "replace").rstrip())
            if not line:
                continue
            low = line.lower()
            if "error" in low or "failed" in low or "invalid" in low:
                flog.warning(line)
            else:
                flog.debug(line)

    # ---------------- 실행 ----------------
    def _on_signal(self, signum, _frame) -> None:
        self.log.info("신호 %s 수신 — ffmpeg 종료", signum)
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # ffmpeg 는 SIGINT 에 깔끔히 마무리한다

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.publish(state="starting")

        try:
            live = config.load_live(self.live_id, server=self.server)
        except config.ConfigError as e:
            self.log.error("설정 오류: %s", e)
            self.publish(state="error", message=f"설정 오류: {e}")
            time.sleep(30)  # 설정 오류로 2초마다 재시작하며 로그를 쏟지 않도록
            return 1
        if not live.enabled:
            self.log.warning("%s 는 enabled: false — 송출하지 않음", live.id)
            self.publish(state="disabled", message="enabled: false")
            return EX_CONFIG

        db.init(self.paths.db)
        conn = db.connect(self.paths.db)

        # 1) concat 목록 (지금부터 24시간)
        try:
            list_path = concat.generate(live, self.paths, conn, self.server)
            self.meta_path = list_path.with_suffix(".json")
        except concat.ConcatError as e:
            self.log.error("concat 목록 생성 실패: %s", e)
            self.publish(state="error", message=str(e))
            db.add_event(conn, "error", "runner", f"concat 생성 실패: {e}", live.id)
            time.sleep(30)
            return 1

        # 2) 스트림 키 (평문은 메모리에만. 로그에는 마스킹)
        try:
            key = secrets.read_stream_key(live.stream_key_file)
        except (FileNotFoundError, ValueError) as e:
            self.log.error("%s", e)
            self.publish(state="error", message="스트림 키 없음")
            time.sleep(30)
            return 1
        base = (live.output_url or self.server["output"]["default_url"]).rstrip("/")
        output_url = f"{base}/{key}"

        # 3) 오디오 대기
        audio_url = f"http://127.0.0.1:{live.harbor_port}/{live.id}.aac"
        timeout = float(self.server["output"]["audio_connect_timeout"])
        if not wait_for_port(live.harbor_port, timeout, self.log):
            self.log.error("오디오 harbor(port %d)가 %ds 안에 열리지 않음 — live-audio@%s 확인", live.harbor_port, timeout, live.id)
            self.publish(state="error", message="오디오 소스 없음")
            return 1

        # 4) ffmpeg
        args = ffmpeg_cmd.build(list_path, audio_url, output_url)  # 내부에서 assert_copy_only
        self.log.info("ffmpeg 시작: %s", ffmpeg_cmd.redacted(args, key))
        self.log.info("bitrate(설정)=%dk tz=%s restart_at=%s concat=%s", live.bitrate_kbps, live.timezone,
                      live.restart_at, list_path.name)
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self.publish(state="running", ffmpeg_pid=self.proc.pid, concat=str(list_path), speed=None,
                     ffmpeg_started_at=time.time(), message="")
        db.add_event(conn, "info", "runner", f"송출 시작 (pid {self.proc.pid}, {list_path.name})", live.id)

        t_err = threading.Thread(target=self._read_stderr, daemon=True)
        t_err.start()
        self._read_progress()
        try:
            rc = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = self.proc.wait()
        t_err.join(timeout=2)

        msg = f"ffmpeg 종료 rc={rc}" + (" (요청에 의한 정지)" if self.stopping else "")
        (self.log.info if self.stopping else self.log.error)(msg)
        self.publish(state="stopped", exit_code=rc, stopped_at=time.time(), message=msg)
        db.add_event(conn, "info" if self.stopping else "error", "runner", msg, live.id)
        conn.close()
        if self.stopping:
            return 0
        return rc or 1  # 목록 끝까지 재생(rc=0)해도 systemd 가 다시 띄우도록 비정상 종료로 보고


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LiveMax 라이브 송출 (ffmpeg copy 먹싱)")
    ap.add_argument("live_id")
    a = ap.parse_args(argv)
    return Runner(a.live_id).run()


if __name__ == "__main__":
    sys.exit(main())
