"""RTMPS TLS 중계 (stunnel) — 송출 트래픽 약 30% 절감.

문제 (live1 실측):
  ffmpeg 내장 RTMPS 는 RTMP 조각(128바이트)마다 TLS 레코드를 따로 만든다. 레코드마다 수십 바이트의
  포장(헤더·인증태그)이 붙어, ffmpeg 출력 5.67 Mbps 가 회선에서는 약 8.4 Mbps 가 됐다 (+48%).
  로컬 재현: FLV 15.82MB → RTMPS 전송 21.39MB (+35%, TCP 헤더 제외).

해결:
  ffmpeg 는 서버 안(127.0.0.1)으로 평문 RTMP 를 보내고, stunnel 이 받아서 큰 TLS 레코드로 묶어
  유튜브로 보낸다. 암호화(RTMPS)와 인증서 검증은 그대로다. 로컬 재현: +2.3%.

설계 원칙:
  - 라이브마다 자기 stunnel 을 runner 의 자식 프로세스로 띄운다. 공용 중계기를 두면 그게 재시작될 때
    모든 라이브가 동시에 끊긴다 (DESIGN 1.3 독립성, 11-6 동시 재시작 금지 위배).
  - 중계를 못 띄우면 기존 직접 RTMPS 로 자동 전환한다 (트래픽은 늘어도 송출은 계속).
  - 유튜브가 받는 정보는 직접 RTMPS 와 같게 맞춘다: SNI = 인제스트 호스트, tcUrl = rtmps://host:443/app.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("livemax.tls_relay")
STUNNEL = shutil.which("stunnel4") or shutil.which("stunnel")
CA_FILE = "/etc/ssl/certs/ca-certificates.crt"


@dataclass
class OutputPlan:
    url: str  # ffmpeg 에 넘길 출력 URL (스트림 키 포함 — 로그 금지)
    extra_args: list[str]  # 출력 옵션 (예: -rtmp_tcurl)
    mode: str  # "rtmps-relay" | "rtmps-direct" | "direct"


def relay_target(base_url: str) -> tuple[str, int, str] | None:
    """rtmps://host[:port]/app → (host, port, '/app'). rtmps 가 아니면 None."""
    u = urlparse(base_url)
    if u.scheme != "rtmps" or not u.hostname:
        return None
    return u.hostname, u.port or 443, u.path.rstrip("/") or "/"


def plan(base_url: str, key: str, relay_port: int | None) -> OutputPlan:
    """중계 포트가 있으면 중계 경유, 없으면 직접. (순수 함수 — 테스트 가능)"""
    base = base_url.rstrip("/")
    t = relay_target(base)
    if t and relay_port:
        host, port, path = t
        return OutputPlan(
            url=f"rtmp://127.0.0.1:{relay_port}{path}/{key}",
            extra_args=["-rtmp_tcurl", f"rtmps://{host}:{port}{path}"],
            mode="rtmps-relay",
        )
    return OutputPlan(url=f"{base}/{key}", extra_args=[], mode="rtmps-direct" if t else "direct")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def is_listening(port: int) -> bool:
    """/proc/net/tcp 로 LISTEN 여부 확인 (접속을 열지 않음 — 유튜브로 빈 연결이 나가지 않게)."""
    want = f"0100007F:{port:04X}"
    try:
        with open("/proc/net/tcp") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 3 and parts[1] == want and parts[3] == "0A":
                    return True
    except OSError:
        pass
    return False


def spread_index(live_id: str) -> int:
    """라이브마다 고정된 번호 (live13 → 13). 숫자가 없으면 이름으로."""
    import re
    import zlib

    d = re.sub(r"\D", "", live_id or "")
    return int(d) if d else zlib.crc32((live_id or "").encode())


def resolve_ipv4(host: str, port: int, spread: str | None = None) -> str | None:
    """유튜브 인제스트의 IPv4 주소. (v1 live1: IPv6 경로가 약 10분마다 끊겨 송출이 멈췄다)

    spread(라이브 ID)를 주면 DNS 가 준 IP 여러 개에 라이브를 **골고루** 나눈다 (v2.18). 예전엔 DNS 가 맨 앞에 준 IP 하나를
    써서 비슷한 시각에 켜진 라이브들이 한 곳에 몰렸고(실측: IP 하나에 13개), 그 유튜브 수신 서버가 몇십 초 멈칫하면
    몰린 라이브들이 한꺼번에 밀렸다 (11-12 00:30 KST 7개 · 2개 재접속). 나눠 두면 한 곳이 멈춰도 영향이 1/IP 수로 준다.
    IP 를 정렬해서 고르므로 DNS 가 답하는 순서가 바뀌어도 같은 라이브는 같은 자리."""
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    if spread is None:
        return infos[0][4][0]
    addrs = sorted(set(i[4][0] for i in infos), key=lambda a: tuple(int(x) for x in a.split(".")))
    return addrs[spread_index(spread) % len(addrs)]


def render_conf(listen_port: int, host: str, port: int, log_path: Path, verify: bool = True,
                connect_addr: str | None = None) -> str:
    lines = [
        "; LiveMax 자동 생성 — 라이브 1개 전용 TLS 중계 (runner 가 매 시작마다 만든다)",
        "foreground = yes",
        "pid =",
        "syslog = no",
        f"output = {log_path}",
        "debug = notice",
        "",
        "[rtmps]",
        "client = yes",
        f"accept = 127.0.0.1:{listen_port}",
        # IPv4 고정 시 주소를 직접 쓰고, 인증서 검증·SNI 는 호스트 이름으로 한다
        f"connect = {connect_addr or host}:{port}",
        *([] if connect_addr else ["delay = yes"]),
        f"sni = {host}",
        "socket = r:SO_KEEPALIVE=1",
        "socket = r:TCP_KEEPIDLE=30",
        "socket = r:TCP_KEEPINTVL=10",
        "TIMEOUTclose = 0",
    ]
    if verify:
        lines += ["verifyChain = yes", f"CAfile = {CA_FILE}", f"checkHost = {host}"]
    return "\n".join(lines) + "\n"


class Relay:
    """라이브 1개의 stunnel 자식 프로세스."""

    def __init__(self, live_id: str, run_dir: Path, log_dir: Path):
        self.connect_addr: str | None = None
        self.live_id = live_id
        self.conf_path = run_dir / f"{live_id}.stunnel.conf"
        self.log_path = log_dir / f"tls-{live_id}.log"
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None

    def start(self, base_url: str, verify: bool = True, wait_s: float = 5.0, ipv4: bool = True) -> int | None:
        t = relay_target(base_url)
        if not t:
            return None
        if not STUNNEL:
            log.warning("%s: stunnel 이 설치되어 있지 않음 — 직접 RTMPS 로 송출 (트래픽 +30%%)", self.live_id)
            return None
        host, port, _ = t
        self.port = free_port()
        self.connect_addr = resolve_ipv4(host, port, spread=self.live_id) if ipv4 else None
        self.conf_path.write_text(render_conf(self.port, host, port, self.log_path, verify, self.connect_addr),
                                  encoding="utf-8")
        self.proc = subprocess.Popen([STUNNEL, str(self.conf_path)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            if is_listening(self.port):
                log.info("%s: TLS 중계 시작 (stunnel pid %d, 127.0.0.1:%d → %s:%d %s)",
                         self.live_id, self.proc.pid, self.port, host, port,
                         f"[IPv4 {self.connect_addr}]" if self.connect_addr else "")
                return self.port
            time.sleep(0.1)
        log.warning("%s: TLS 중계를 띄우지 못함 — 직접 RTMPS 로 송출 (로그: %s)", self.live_id, self.log_path)
        self.stop()
        return None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.conf_path.unlink(missing_ok=True)
