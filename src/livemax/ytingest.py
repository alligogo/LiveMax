"""유튜브 수신 서버(IP) 모음 (ytingest — 업로드 처리 ingest.py 와 별개) + 가장 적게 붙은 IP 로 붙기 (v2.19, DESIGN 11-G).

왜: 중계기가 켤 때 DNS 맨 앞 IP 하나를 쓰면 비슷한 시각에 켜진 라이브들이 한 IP 에 몰리고(실측 13개), 그 유튜브 수신 서버가
몇십 초 멈칫하면 몰린 라이브가 한꺼번에 밀린다 (11-12 00:30 KST 7개). v2.18 은 'DNS 가 지금 준 IP' 에 고르게 나눴지만
DNS 가 IP 를 2개만 주는 때가 있어 25/25 로 몰렸다. 그래서:

  - 1시간마다 DNS 가 준 IPv4 를 **모아 두고**(48시간 넘게 다시 안 보이면 버림), IP 마다 TLS 인사만 해 보고 닫아 살아 있는지 확인
    (스트림 키는 보내지 않음 — 유튜브 입장에선 브라우저가 한 번 접속했다 나간 정도).
  - 라이브가 붙을 때(처음·다시 접속 모두) **지금 연결이 가장 적은 IP** 로. 접속 때 TLS 인증서를 호스트 이름으로 확인하므로
    유튜브가 아닌 IP 에는 붙지 않는다. 라이브 1개 = 연결 1개는 그대로 — 어느 IP 에 붙느냐만 바뀐다.
  - 중계기(별도 프로세스)는 다시 접속할 때 run/ingest_load.json 을 읽어 가장 적게 붙은 IP 를 고른다.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import threading
import time
from pathlib import Path

from .paths import Paths

log = logging.getLogger("livemax.ytingest")

POOL_TTL_S = 48 * 3600   # DNS 에서 이만큼 다시 안 보이면 모음에서 뺀다
REFRESH_S = 3600         # DNS 다시 묻기·살아 있는지 확인 간격
RESERVE_S = 120          # 고른 뒤 중계기가 '붙었다' 고 알려 오기 전까지 그 IP 몫으로 셈 (부팅 때 줄줄이 켜도 몰리지 않게)
LOAD_WRITE_S = 10        # 중계기용 현재 분포 파일 갱신 간격


def ip_key(a: str) -> tuple:
    try:
        return tuple(int(x) for x in a.split("."))
    except ValueError:
        return (999,)


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos}, key=ip_key)


def _tls_ok(ip: str, host: str, port: int, cafile: str | None = None, timeout: float = 5.0) -> bool:
    """TLS 인사만 해 보고 닫는다 (인증서는 호스트 이름으로 확인 — 진짜 유튜브 수신 서버인지)."""
    try:
        ctx = ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)
        with socket.create_connection((ip, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host):
                return True
    except (OSError, ssl.SSLError):
        return False


class IngestPool:
    def __init__(self, paths: Paths, peers_fn=None, resolve=None, tls_ok=None, cafile: str | None = None):
        self.file = paths.state / "ingest_pool.json"        # 모음 (재시작해도 남김)
        self.load_file = paths.run_dir / "ingest_load.json"  # 중계기용 지금 분포
        self.peers_fn = peers_fn or (lambda: {})            # () -> {라이브 ID: 지금 붙은 IP}
        self._resolve = resolve or _resolve
        self._tls_ok = tls_ok or (lambda ip, host, port: _tls_ok(ip, host, port, cafile))
        self.lock = threading.Lock()
        self.hosts: dict[str, dict[str, dict]] = {}  # "host:port" → {IP: {"seen", "ok", "checked"}}
        self.reserved: dict[str, tuple[str, float]] = {}  # 라이브 ID → (고른 IP, 시각)
        self._load()

    # ---------------------------------------------------------------- 저장
    def _load(self) -> None:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                self.hosts = {k: dict(v) for k, v in d.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.hosts), encoding="utf-8")
            tmp.replace(self.file)
        except OSError:
            log.debug("수신 IP 모음 저장 실패", exc_info=True)

    # ---------------------------------------------------------------- 모으기·확인
    def refresh(self, host: str, port: int, check: bool = True, now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = f"{host}:{port}"
        try:
            fresh = self._resolve(host, port)
        except OSError:
            fresh = []
        with self.lock:
            pool = self.hosts.setdefault(key, {})
            for ip in fresh:
                e = pool.setdefault(ip, {"seen": now, "ok": True, "checked": None})  # None = 아직 확인 안 함
                e["seen"] = now
            for ip in [ip for ip, e in pool.items() if now - float(e.get("seen", 0)) > POOL_TTL_S]:
                del pool[ip]
            todo = [ip for ip, e in pool.items()
                    if check and (e.get("checked") is None or now - float(e["checked"]) >= REFRESH_S - 60)]
        for ip in todo:
            ok = bool(self._tls_ok(ip, host, port))
            with self.lock:
                e = self.hosts.get(key, {}).get(ip)
                if e is not None:
                    if e.get("ok", True) != ok:
                        log.info("유튜브 수신 서버 %s %s", ip, "다시 정상" if ok else "응답 없음 — 당분간 안 씀")
                    e["ok"], e["checked"] = ok, now
        with self.lock:
            self._save()

    def healthy(self, host: str, port: int) -> list[str]:
        with self.lock:
            pool = self.hosts.get(f"{host}:{port}", {})
            return sorted((ip for ip, e in pool.items() if e.get("ok", True)), key=ip_key)

    # ---------------------------------------------------------------- 분포·고르기
    def load(self, now: float | None = None) -> dict[str, int]:
        """IP → 지금 붙어 있는(또는 막 고른) 라이브 수."""
        now = time.time() if now is None else now
        peers = dict(self.peers_fn() or {})
        cnt: dict[str, int] = {}
        with self.lock:
            for lid, (ip, t) in list(self.reserved.items()):
                if now - t > RESERVE_S:
                    del self.reserved[lid]
                elif lid not in peers:
                    peers[lid] = ip
        for ip in peers.values():
            if ip:
                cnt[ip] = cnt.get(ip, 0) + 1
        return cnt

    def pick(self, live_id: str, host: str, port: int, exclude: str | None = None) -> str | None:
        """가장 적게 붙은 살아 있는 IP. 같으면 라이브 번호로 돌려 가며 (늘 같은 IP 부터 차지 않게). 모음이 비면 None."""
        from .tls_relay import spread_index

        if not self.healthy(host, port):
            self.refresh(host, port, check=False)  # 처음: DNS 만 묻고 바로 (TLS 확인은 1시간 주기에서)
        ips = [ip for ip in self.healthy(host, port) if ip != exclude] or self.healthy(host, port)
        if not ips:
            return None
        cnt = self.load()
        cnt.pop(None, None)
        if live_id in self.peers_fn():  # 다시 고르는 라이브 자신의 몫은 빼고 센다
            cur = self.peers_fn()[live_id]
            if cur in cnt:
                cnt[cur] -= 1
        low = min(cnt.get(ip, 0) for ip in ips)
        ties = [ip for ip in ips if cnt.get(ip, 0) == low]
        ip = ties[spread_index(live_id) % len(ties)]
        with self.lock:
            self.reserved[live_id] = (ip, time.time())
        self.write_load()
        return ip

    def write_load(self) -> None:
        """중계기(별도 프로세스)가 다시 접속할 때 읽는 지금 분포."""
        cnt = self.load()
        with self.lock:
            keys = list(self.hosts)
        d = {"ts": time.time(), "hosts": {}}
        for key in keys:
            host, _, port = key.rpartition(":")
            ips = self.healthy(host, int(port or 0))
            d["hosts"][key] = {"ips": ips, "load": {ip: cnt.get(ip, 0) for ip in ips}}
        try:
            self.load_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.load_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            tmp.replace(self.load_file)
        except OSError:
            pass

    def summary(self) -> dict:
        """서버 박스용: 쓸 수 있는 IP 수 · IP 별 붙은 수 · 한 곳 최대."""
        cnt = self.load()
        with self.lock:
            keys = list(self.hosts)
        ips: set[str] = set()
        for key in keys:
            host, _, port = key.rpartition(":")
            ips.update(self.healthy(host, int(port or 0)))
        used = {ip: n for ip, n in cnt.items() if ip}
        return {"ips": len(ips), "used": len(used), "load": dict(sorted(used.items(), key=lambda x: -x[1])),
                "max": max(used.values()) if used else 0, "total": sum(used.values())}

    def run(self, stop: threading.Event) -> None:
        last = 0.0
        while not stop.wait(LOAD_WRITE_S):
            try:
                now = time.time()
                if now - last >= REFRESH_S:
                    last = now
                    with self.lock:
                        keys = list(self.hosts)
                    for key in keys:
                        host, _, port = key.rpartition(":")
                        self.refresh(host, int(port or 0))
                self.write_load()
            except Exception:  # noqa: BLE001 — 모음 갱신 실패가 송출을 막지 않게
                log.exception("수신 IP 모음 갱신 실패")


def pick_from_file(load_file: str | Path, host: str, port: int, current: str | None) -> str | None:
    """중계기(별도 프로세스) 쪽: 다시 접속할 때 지금 분포 파일에서 가장 적게 붙은 IP (지금 IP 제외)."""
    try:
        d = json.loads(Path(load_file).read_text(encoding="utf-8"))
        e = d["hosts"][f"{host}:{port}"]
        ips, load = list(e.get("ips") or []), dict(e.get("load") or {})
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if current in load:
        load[current] = max(0, int(load[current]) - 1)
    cand = [ip for ip in ips if ip != current]
    if not cand:
        return None
    return min(cand, key=lambda ip: (int(load.get(ip, 0)), ip_key(ip)))
