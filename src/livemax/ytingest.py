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
NEAR_MS = 15             # 가까운 IP: 가장 빠른 곳의 2배 이내 또는 이 값 이하 (v2.22 — 먼 IP 65ms 에서 시청자 영향 사건 두 번)
NEAR_X = 2.0
SITE_GAP_X, SITE_GAP_MS = 1.5, 1.0  # v2.23 지역 묶기: 왕복 시간을 정렬해 앞 IP 의 1.5배 + 1ms 를 넘으면 다른 지역
                                     # (11-14 02:23: 9ms 대 3곳이 같은 순간 18~27ms 로 — 같은 지역, 2.4ms 1곳은 멀쩡)
HEALTHY_X, HEALTHY_MS = 1.6, 5.0     # 지금 왕복이 기준의 1.6배·+5ms 를 넘으면 그 지역은 '막힘' (옮길 곳으로 안 고름)
TRACE_X, TRACE_MS = 2.0, 8.0         # E: 지금 왕복이 기준의 2배이면서 +8ms 넘게 15초 두 번 → 길 추적 저장
TRACE_COOLDOWN_S = 1800
MIN_IPS = 4              # 모음이 이보다 적으면 켜기 전에 넉넉히 모은다 (v2.20 — 업데이트 순간 DNS 가 1개만 줘서 50개가 한 IP 로 간 일)
BOOT_COOLDOWN_S = 600    # 넉넉히 모으기는 이 간격 안에 다시 안 함
DOH = ("https://dns.google/resolve?name={host}&type=A",            # 구글 공개 DNS (우리 서버 위치 기준으로 답함)
       "https://cloudflare-dns.com/dns-query?name={host}&type=A")  # 클라우드플레어


def ip_key(a: str) -> tuple:
    try:
        return tuple(int(x) for x in a.split("."))
    except ValueError:
        return (999,)


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos}, key=ip_key)


def _doh(host: str, timeout: float = 3.0) -> list[str]:
    """공개 DNS(HTTPS)에도 물어 IP 를 더 모은다 — 시스템 DNS 가 1~2개만 줄 때. 받은 IP 는 접속 때 인증서로 진짜 유튜브인지 확인된다."""
    import urllib.request

    out: set[str] = set()
    for url in DOH:
        try:
            req = urllib.request.Request(url.format(host=host), headers={"accept": "application/dns-json", "user-agent": "livemax"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            out.update(a["data"] for a in d.get("Answer", []) if a.get("type") == 1 and a.get("data"))
        except Exception:  # noqa: BLE001 — 공개 DNS 가 안 되면 시스템 DNS 만으로
            continue
    return sorted(out, key=ip_key)


def _tls_ok(ip: str, host: str, port: int, cafile: str | None = None, timeout: float = 5.0) -> tuple[bool, float | None]:
    """TLS 인사만 해 보고 닫는다 (인증서는 호스트 이름으로 확인 — 진짜 유튜브 수신 서버인지).
    왕복 시간: TCP 접속에 걸린 시간 3번의 가운데 값 (ms) — 가깝고 빠른 IP 를 고르는 데 쓴다 (v2.22)."""
    try:
        ctx = ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)
        rtts = []
        for i in range(3):
            t0 = time.monotonic()
            with socket.create_connection((ip, port), timeout=timeout) as s:
                rtts.append((time.monotonic() - t0) * 1000)
                if i == 0:
                    with ctx.wrap_socket(s, server_hostname=host):
                        pass
        return True, round(sorted(rtts)[1], 1)
    except (OSError, ssl.SSLError):
        return False, None


def _norm(res) -> tuple[bool, float | None]:
    """tls_ok 결과: (성공, 왕복 ms) — 예전 꼴(성공만)도 받는다."""
    if isinstance(res, tuple):
        return bool(res[0]), (float(res[1]) if res[1] is not None else None)
    return bool(res), None


class IngestPool:
    def __init__(self, paths: Paths, peers_fn=None, resolve=None, tls_ok=None, cafile: str | None = None, doh=None,
                 sleep=time.sleep, tracer=None):
        self.file = paths.state / "ingest_pool.json"        # 모음 (재시작해도 남김)
        self.load_file = paths.run_dir / "ingest_load.json"  # 중계기용 지금 분포
        self.peers_fn = peers_fn or (lambda: {})            # () -> {라이브 ID: 지금 붙은 IP}
        self._resolve = resolve or _resolve
        self._tls_ok = tls_ok or (lambda ip, host, port: _tls_ok(ip, host, port, cafile))
        self.lock = threading.Lock()
        self.hosts: dict[str, dict[str, dict]] = {}  # "host:port" → {IP: {"seen", "ok", "checked"}}
        self.reserved: dict[str, tuple[str, float]] = {}  # 라이브 ID → (고른 IP, 시각)
        self._doh = doh if doh is not None else _doh
        self._sleep = sleep
        self._boot_lock = threading.Lock()
        self._boot_at: dict[str, float] = {}
        self.rtt_now_fn = lambda: {}  # () -> {IP: 최근 15초 평균 왕복 ms} — 수집기가 끼워 넣는다 (중계기 실측)
        self.paths = paths
        self._tracer = tracer or _trace
        self._spike: dict[str, int] = {}
        self._traced_at: dict[str, float] = {}
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
            ok, rtt = _norm(self._tls_ok(ip, host, port))
            with self.lock:
                e = self.hosts.get(key, {}).get(ip)
                if e is not None:
                    if e.get("ok", True) != ok:
                        log.info("유튜브 수신 서버 %s %s", ip, "다시 정상" if ok else "응답 없음 — 당분간 안 씀")
                    e["ok"], e["checked"] = ok, now
                    if rtt is not None:
                        e["rtt"] = rtt
        with self.lock:
            self._save()

    def ensure(self, host: str, port: int) -> None:
        """모음이 MIN_IPS 보다 적으면 넉넉히 모은다: 시스템 DNS 를 몇 번 더 묻고 공개 DNS 에도 묻고, 새 IP 는 TLS 인사로 바로 확인.
        업데이트 직후 처음 고를 때 한 번 (몇 초) — 그 사이 다른 라이브는 기다렸다가 같은 결과를 쓴다."""
        key = f"{host}:{port}"
        if len(self.healthy(host, port)) >= MIN_IPS:
            return
        with self._boot_lock:
            if len(self.healthy(host, port)) >= MIN_IPS or time.time() - self._boot_at.get(key, 0) < BOOT_COOLDOWN_S:
                return
            self._boot_at[key] = time.time()
            found: set[str] = set()
            for i in range(3):
                try:
                    found.update(self._resolve(host, port))
                except OSError:
                    pass
                if i < 2:
                    self._sleep(0.5)
            try:
                found.update(self._doh(host) or [])
            except Exception:  # noqa: BLE001
                pass
            now = time.time()
            with self.lock:
                pool = self.hosts.setdefault(key, {})
                new = [ip for ip in found if ip not in pool]
                for ip in found:
                    pool.setdefault(ip, {"seen": now, "ok": True, "checked": None})["seen"] = now
            if new:  # 새로 찾은 IP 는 바로 TLS 인사 (동시에, 몇 초 안에)
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=8) as ex:
                    res = dict(zip(new, ex.map(lambda ip: _norm(self._tls_ok(ip, host, port)), new)))
                with self.lock:
                    for ip, (ok, rtt) in res.items():
                        e = self.hosts[key].get(ip)
                        if e is not None:
                            e["ok"], e["checked"] = ok, now
                            if rtt is not None:
                                e["rtt"] = rtt
            with self.lock:
                self._save()
            log.info("유튜브 수신 IP 넉넉히 모음: %d곳 (새로 %d곳)", len(self.healthy(host, port)), len(new))

    def rtt_of(self, host: str, port: int) -> dict[str, float]:
        with self.lock:
            pool = self.hosts.get(f"{host}:{port}", {})
            return {ip: float(e["rtt"]) for ip, e in pool.items() if e.get("ok", True) and e.get("rtt") is not None}

    def near(self, host: str, port: int) -> list[str]:
        """가깝고 빠른 IP (v2.22): 가장 빠른 곳의 2배 이내 또는 15ms 이하. 왕복 시간을 아직 모르면 살아 있는 IP 전부.
        가까운 곳이 1곳뿐이어도 먼 곳으로 나누지 않는다 — 분산보다 가깝고 빠른 게 중요 (사용자 결정)."""
        ips = self.healthy(host, port)
        rtt = {ip: v for ip, v in self.rtt_of(host, port).items() if ip in ips}
        if not rtt:
            return ips
        best = min(rtt.values())
        lim = max(NEAR_MS, best * NEAR_X)
        return sorted((ip for ip, v in rtt.items() if v <= lim), key=lambda ip: (rtt[ip], ip_key(ip)))

    def sites(self, host: str, port: int) -> dict[str, int]:
        """IP → 지역 번호 (0 = 가장 빠른 지역). 왕복 시간을 정렬해 크게 벌어지는 곳에서 끊는다 (v2.23)."""
        rtt = self.rtt_of(host, port)
        out: dict[str, int] = {}
        sid, prev = -1, None
        for ip in sorted(rtt, key=lambda x: (rtt[x], ip_key(x))):
            if prev is None or rtt[ip] > prev * SITE_GAP_X + SITE_GAP_MS:
                sid += 1
            out[ip] = sid
            prev = rtt[ip]
        return out

    def site_label(self, host: str, port: int) -> dict[str, str]:
        """IP → '지역 A (2ms대)' 처럼 사람이 읽는 이름."""
        rtt, st = self.rtt_of(host, port), self.sites(host, port)
        first: dict[int, float] = {}
        for ip, sid in st.items():
            first[sid] = min(first.get(sid, 1e9), rtt[ip])
        return {ip: f"지역 {chr(65 + sid)} ({int(first[sid])}ms대)" for ip, sid in st.items()}

    def _measure_missing(self, host: str, port: int) -> None:
        """왕복 시간을 모르는 살아 있는 IP 를 바로 잰다 (처음 고를 때 — 동시에, 1초 안팎)."""
        key = f"{host}:{port}"
        with self.lock:
            miss = [ip for ip, e in self.hosts.get(key, {}).items() if e.get("ok", True) and e.get("rtt") is None]
        if not miss:
            return
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as ex:
            res = dict(zip(miss, ex.map(lambda ip: _norm(self._tls_ok(ip, host, port)), miss)))
        now = time.time()
        with self.lock:
            for ip, (ok, rtt) in res.items():
                e = self.hosts.get(key, {}).get(ip)
                if e is not None:
                    e["ok"], e["checked"] = ok, now
                    if rtt is not None:
                        e["rtt"] = rtt
            self._save()

    def should_move(self, ip: str | None) -> bool:
        """자정 재시작 때 이 IP 의 라이브를 덜 붐비는 IP 로 옮길까 (v2.20 — 업데이트 없이도 매일 밤 고르게).
        옮기면 이 IP 는 1 줄고 다른 곳은 1 늘므로, 차이가 2 이상일 때만."""
        if not ip:
            return False
        with self.lock:
            keys = [k for k, pool in self.hosts.items() if ip in pool]
        cnt = self.load()
        for key in keys:
            host, _, port = key.rpartition(":")
            near = self.near(host, int(port or 0))
            if near and ip not in near:  # 먼 IP 에 붙어 있음 → 가까운 곳으로 (v2.22)
                return True
            st = self.sites(host, int(port or 0))
            sload: dict[int, int] = {}
            for x in near:
                sload[st.get(x, 0)] = sload.get(st.get(x, 0), 0) + cnt.get(x, 0)
            mine = st.get(ip, 0)
            others_s = [n for sid, n in sload.items() if sid != mine]
            if others_s and sload.get(mine, 0) - min(others_s) >= 2:  # 지역끼리 고르게 (v2.23)
                return True
            same = [x for x in near if x != ip and st.get(x, 0) == mine]
            if same and cnt.get(ip, 0) - min(cnt.get(x, 0) for x in same) >= 2:
                return True
        return False

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
        self.ensure(host, port)  # 모음이 적으면 넉넉히 (v2.20)
        self._measure_missing(host, port)  # 왕복 시간 모르는 IP 를 바로 재고 (v2.22)
        near = self.near(host, port)
        ips = [ip for ip in near if ip != exclude] or near
        if not ips:
            return None
        rtt = self.rtt_of(host, port)
        cnt = self.load()
        cnt.pop(None, None)
        if live_id in self.peers_fn():  # 다시 고르는 라이브 자신의 몫은 빼고 센다
            cur = self.peers_fn()[live_id]
            if cur in cnt:
                cnt[cur] -= 1
        st = self.sites(host, port)
        if len({st.get(ip, 0) for ip in ips}) > 1:  # v2.23: 지역끼리 먼저 고르게 (한 지역이 막혀도 절반은 멀쩡)
            sload: dict[int, int] = {}
            for x in ips:
                sload[st.get(x, 0)] = sload.get(st.get(x, 0), 0) + cnt.get(x, 0)
            low_s = min(sload.values())
            best_sites = [sid for sid, n in sload.items() if n == low_s]
            ips = [ip for ip in ips if st.get(ip, 0) == min(best_sites)]
        low = min(cnt.get(ip, 0) for ip in ips)
        ties = [ip for ip in ips if cnt.get(ip, 0) == low]
        if rtt:  # 같으면 더 빠른 곳 (왕복 시간 차이 1ms 안은 같은 걸로 보고 라이브 번호로 돌림)
            fastest = min(rtt.get(ip, 1e9) for ip in ties)
            ties = [ip for ip in ties if rtt.get(ip, 1e9) <= fastest + 1.0]
        ip = ties[spread_index(live_id) % len(ties)]
        with self.lock:
            self.reserved[live_id] = (ip, time.time())
        self.write_load()
        return ip

    def write_load(self) -> None:
        """중계기(별도 프로세스)가 다시 접속할 때 읽는 지금 분포."""
        cnt = self.load()
        try:
            now_rtt = dict(self.rtt_now_fn() or {})
        except Exception:  # noqa: BLE001
            now_rtt = {}
        with self.lock:
            keys = list(self.hosts)
        d = {"ts": time.time(), "hosts": {}}
        for key in keys:
            host, _, port = key.rpartition(":")
            ips = self.near(host, int(port or 0))
            st = self.sites(host, int(port or 0))
            base = self.rtt_of(host, int(port or 0))
            d["hosts"][key] = {"ips": ips, "load": {ip: cnt.get(ip, 0) for ip in ips},
                               "site": {ip: st.get(ip, 0) for ip in ips},
                               "rtt_base": {ip: base[ip] for ip in ips if ip in base},
                               "rtt_now": {ip: now_rtt[ip] for ip in ips if ip in now_rtt}}
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
        near: set[str] = set()
        rtt: dict[str, float] = {}
        for key in keys:
            host, _, port = key.rpartition(":")
            ips.update(self.healthy(host, int(port or 0)))
            near.update(self.near(host, int(port or 0)))
            rtt.update(self.rtt_of(host, int(port or 0)))
        used = {ip: n for ip, n in cnt.items() if ip}
        total, mx = sum(used.values()), max(used.values()) if used else 0
        # 심하게 몰림 (v2.21): 유튜브가 준 IP 로 고르게 나눈 수보다 눈에 띄게 많을 때만. IP 가 적은 건 유튜브가 정하는 일이라 경고 안 함
        site_of: dict[str, str] = {}
        sload: dict[str, int] = {}
        for key in keys:
            host, _, port = key.rpartition(":")
            lab = self.site_label(host, int(port or 0))
            site_of.update(lab)
            for ip in self.near(host, int(port or 0)):
                sload[lab.get(ip, ip)] = sload.get(lab.get(ip, ip), 0) + used.get(ip, 0)
        # 몰림 판정은 지역 단위 (v2.23): 지역이 1곳이면 판정 안 함
        fair = -(-total // max(1, len(sload))) if total else 0
        smax = max(sload.values()) if sload else 0
        crowded = bool(total) and len(sload) >= 2 and smax > fair + max(3, -(-fair // 4))
        return {"ips": len(ips), "near": len(near), "used": len(used), "load": dict(sorted(used.items(), key=lambda x: -x[1])),
                "max": mx, "total": total, "fair": fair, "crowded": crowded,
                "far_used": sum(n for ip, n in used.items() if near and ip not in near), "rtt": rtt,
                "site_of": site_of, "site_load": sload, "site_max": smax}

    def check_spikes(self, now: float | None = None) -> list[str]:
        """E (v2.23): 어떤 IP 의 지금 왕복이 기준의 2배이면서 +8ms 넘게 두 번 이어지면 그 길을 추적해 저장 (30분에 한 번).
        비교용으로 멀쩡한 다른 지역 IP 도 같이 — 막힌 곳이 OVH 안·중간 통신사·구글 입구 중 어디인지 가린다. 추적은 따로 돌아 송출과 무관."""
        now = time.time() if now is None else now
        try:
            cur = dict(self.rtt_now_fn() or {})
        except Exception:  # noqa: BLE001
            return []
        started = []
        with self.lock:
            keys = list(self.hosts)
        for key in keys:
            host, _, port = key.rpartition(":")
            base = self.rtt_of(host, int(port or 0))
            st = self.sites(host, int(port or 0))
            for ip, b in base.items():
                v = cur.get(ip)
                bad = v is not None and v >= max(b * TRACE_X, b + TRACE_MS)
                self._spike[ip] = self._spike.get(ip, 0) + 1 if bad else 0
                if self._spike[ip] >= 2 and now - self._traced_at.get(ip, 0) >= TRACE_COOLDOWN_S:
                    self._traced_at[ip] = now
                    ok = [x for x in base if st.get(x) != st.get(ip) and not (cur.get(x) and cur[x] >= max(base[x] * TRACE_X, base[x] + TRACE_MS))]
                    ref = min(ok, key=lambda x: base[x]) if ok else None
                    threading.Thread(target=self._trace_job, args=(ip, b, v, ref, base.get(ref) if ref else None),
                                     name=f"trace-{ip}", daemon=True).start()
                    started.append(ip)
        return started

    def _trace_job(self, ip: str, base: float, now_ms: float, ref: str | None, ref_base: float | None) -> None:
        from . import db

        t = time.time()
        texts = [f"# 막힌 곳 후보 {ip}: 평소 {base}ms → 지금 {now_ms}ms\n{self._tracer(ip)}"]
        if ref:
            texts.append(f"# 비교 (멀쩡한 다른 지역) {ref}: 평소 {ref_base}ms\n{self._tracer(ref)}")
        try:
            with db.session(self.paths.db) as c:
                c.execute("INSERT INTO traces(ts, ip, rtt_base, rtt_now, text) VALUES (?,?,?,?,?)",
                          (t, ip, base, now_ms, "\n\n".join(texts)[:20000]))
                c.execute("DELETE FROM traces WHERE ts < ?", (t - 11 * 86400,))
            log.warning("유튜브 수신 %s 왕복 %sms → %sms — 길 추적 저장", ip, base, now_ms)
        except Exception:  # noqa: BLE001
            log.exception("길 추적 저장 실패")

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
                        self.ensure(host, int(port or 0))
                self.write_load()
                self.check_spikes()
            except Exception:  # noqa: BLE001 — 모음 갱신 실패가 송출을 막지 않게
                log.exception("수신 IP 모음 갱신 실패")


def _healthy_ip(e: dict, ip: str) -> bool:
    b, v = (e.get("rtt_base") or {}).get(ip), (e.get("rtt_now") or {}).get(ip)
    return b is None or v is None or v <= max(b * HEALTHY_X, b + HEALTHY_MS)


def pick_from_file(load_file: str | Path, host: str, port: int, current: str | None,
                   avoid_site: bool = False, healthy_only: bool = False) -> str | None:
    """중계기(별도 프로세스) 쪽: 다시 접속·옮길 때 분포 파일에서 고른다 (지금 IP 제외).
    avoid_site: 지금 IP 와 다른 지역만 (v2.23 — 막힌 지역을 건너뜀). healthy_only: 지금 왕복이 정상인 지역만 (다 느리면 None).
    덜 붐비는 지역 → 그 안에서 가장 적게 붙은 IP → 더 빠른 IP."""
    try:
        d = json.loads(Path(load_file).read_text(encoding="utf-8"))
        e = d["hosts"][f"{host}:{port}"]
        ips, load = list(e.get("ips") or []), dict(e.get("load") or {})
    except (OSError, ValueError, KeyError, TypeError):
        return None
    site = dict(e.get("site") or {})
    if current in load:
        load[current] = max(0, int(load[current]) - 1)
    cand = [ip for ip in ips if ip != current]
    if avoid_site and current in site:
        cand = [ip for ip in cand if site.get(ip) != site[current]]
    if healthy_only:
        cand = [ip for ip in cand if _healthy_ip(e, ip)]
    if not cand:
        return None
    sload: dict = {}
    for ip in ips:
        sload[site.get(ip, 0)] = sload.get(site.get(ip, 0), 0) + int(load.get(ip, 0))
    base = dict(e.get("rtt_base") or {})
    return min(cand, key=lambda ip: (sload.get(site.get(ip, 0), 0), int(load.get(ip, 0)), base.get(ip, 1e9), ip_key(ip)))


def _trace(ip: str) -> str:
    """mtr(TCP 443) 보고서 — 홉마다 손실·왕복. 없으면 tracepath. 실패하면 이유만."""
    import shutil
    import subprocess

    for cmd in (["mtr", "-n", "-r", "-c", "10", "-T", "-P", "443", "-Z", "1", ip], ["tracepath", "-n", "-m", "20", ip]):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                return (r.stdout or r.stderr).strip()[:8000]
            except Exception as e:  # noqa: BLE001
                return f"{cmd[0]} 실패: {e}"
    return "길 추적 도구 없음 (mtr·tracepath)"
