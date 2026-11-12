"""v2.19: 유튜브 수신 IP 모음 + 가장 적게 붙은 IP 로 붙기."""

import json

from livemax import ytingest

IPS6 = ["142.250.117.134", "142.250.129.134", "142.250.140.134", "142.250.151.134", "142.251.29.134", "142.251.30.134"]
IPS2 = ["64.233.178.134", "74.125.197.134"]


def _pool(paths, dns, ok=lambda ip: True):
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: list(dns[0]), tls_ok=lambda ip, h, p: ok(ip))
    return pool, peers


def test_collects_ips_across_dns_answers_and_spreads(paths):
    """DNS 가 어제 6개, 오늘 2개를 줘도 모아 둔 8개에 고르게 (v2.18: 지금 준 2개에만 → 25/25)."""
    dns = [IPS6]
    pool, peers = _pool(paths, dns)
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    dns[0] = IPS2
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0 + 3600)
    assert len(pool.healthy("a.rtmps.youtube.com", 443)) == 8
    for i in range(1, 51):  # 부팅 때 줄줄이 (붙었다는 알림은 아직 — 예약으로 셈)
        pool.pick(f"live{i}", "a.rtmps.youtube.com", 443)
    cnt = pool.load()
    assert sum(cnt.values()) == 50 and max(cnt.values()) <= 7 and min(cnt.values()) >= 6
    for lid, (ip, _t) in pool.reserved.items():  # 중계기가 붙었다고 알려 옴
        peers[lid] = ip
    s = pool.summary()
    assert s["ips"] == 8 and s["max"] <= 7 and s["total"] == 50


def test_expired_and_dead_ips_not_used(paths):
    dns = [IPS6]
    pool, _ = _pool(paths, dns, ok=lambda ip: ip != IPS6[0])
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    assert IPS6[0] not in pool.healthy("a.rtmps.youtube.com", 443)  # TLS 인사 실패 → 안 씀
    dns[0] = IPS2
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0 + ytingest.POOL_TTL_S + 10)
    assert pool.healthy("a.rtmps.youtube.com", 443) == IPS2  # 48시간 넘게 안 보인 IP 는 버림
    # 다시 만들어도 모음은 남아 있음 (재시작·업데이트)
    pool2, _ = _pool(paths, [[]])
    assert pool2.healthy("a.rtmps.youtube.com", 443) == IPS2


def test_reconnect_picks_least_loaded_not_next(paths, tmp_path):
    """다시 접속: 지금 IP 를 빼고 가장 적게 붙은 곳 (v2.18 은 '다음' IP → IP 2개면 한쪽으로 우르르)."""
    f = tmp_path / "load.json"
    f.write_text(json.dumps({"hosts": {"a.rtmps.youtube.com:443": {
        "ips": IPS6, "load": {IPS6[0]: 9, IPS6[1]: 9, IPS6[2]: 3, IPS6[3]: 9, IPS6[4]: 9, IPS6[5]: 9}}}}))
    assert ytingest.pick_from_file(f, "a.rtmps.youtube.com", 443, IPS6[0]) == IPS6[2]
    assert ytingest.pick_from_file(f, "a.rtmps.youtube.com", 443, IPS6[2]) == IPS6[0]  # 자기 IP 는 빼고
    assert ytingest.pick_from_file(tmp_path / "없음.json", "a.rtmps.youtube.com", 443, IPS6[0]) is None
