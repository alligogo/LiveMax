"""v2.19: 유튜브 수신 IP 모음 + 가장 적게 붙은 IP 로 붙기."""

import json

from livemax import ytingest

IPS6 = ["142.250.117.134", "142.250.129.134", "142.250.140.134", "142.250.151.134", "142.251.29.134", "142.251.30.134"]
IPS2 = ["64.233.178.134", "74.125.197.134"]


def _pool(paths, dns, ok=lambda ip: True):
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: list(dns[0]), tls_ok=lambda ip, h, p: ok(ip),
                               doh=lambda h: [], sleep=lambda s: None)
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


def test_bootstrap_when_dns_gives_one_ip(paths):
    """v2.20: 업데이트 순간 시스템 DNS 가 IP 를 1개만 줘도 켜기 전에 넉넉히 모은다 (DNS 여러 번 + 공개 DNS)."""
    answers = iter([["74.125.197.134"], ["74.125.197.134"], ["64.233.178.134"], ["74.125.197.134"]])
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: next(answers, ["74.125.197.134"]),
                               tls_ok=lambda ip, h, p: ip != "9.9.9.9", doh=lambda h: IPS6[:4] + ["9.9.9.9"], sleep=lambda s: None)
    for i in range(1, 51):
        pool.pick(f"live{i}", "a.rtmps.youtube.com", 443)
    cnt = pool.load()
    assert "9.9.9.9" not in cnt  # TLS 인사 실패한 IP 는 안 씀
    assert len(cnt) == 6 and max(cnt.values()) <= 9  # 50개 ÷ 6곳


def test_should_move_only_when_crowded(paths):
    dns = [IPS2]
    pool, peers = _pool(paths, dns)
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    peers.update({f"live{i}": IPS2[0] for i in range(10)})
    peers.update({f"live{i}": IPS2[1] for i in range(10, 19)})
    assert not pool.should_move(IPS2[0])  # 10 vs 9 — 옮겨도 9 vs 10, 이득 없음
    peers.update({f"live{i}": IPS2[0] for i in range(19, 22)})
    assert pool.should_move(IPS2[0]) and not pool.should_move(IPS2[1])  # 13 vs 9
    assert not pool.should_move(None)


def test_crowded_only_when_far_above_fair(paths):
    """v2.21: IP 가 2곳뿐이라 25/25 인 건 정상(유튜브가 정하는 일) — 고르게 나눈 수보다 눈에 띄게 몰렸을 때만 '몰림'."""
    pool, peers = _pool(paths, [IPS2])
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    peers.update({f"live{i}": IPS2[i % 2] for i in range(50)})
    s = pool.summary()
    assert (s["fair"], s["max"], s["crowded"]) == (25, 25, False)
    peers.update({f"live{i}": IPS2[0] for i in range(30)})  # 35 / 15
    assert pool.summary()["crowded"] is True
    pool1, peers1 = _pool(paths, [[IPS2[0]]])
    pool1.hosts.clear()
    pool1.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    peers1.update({f"live{i}": IPS2[0] for i in range(50)})
    assert pool1.summary()["crowded"] is False  # IP 가 1곳뿐이면 할 수 있는 게 없음 — 경고 안 함


# 11-13 실측 모음 (왕복 ms) — 가까운 4곳 · 먼 4곳(65ms 대: 시청자 영향 사건 두 번 모두 142.250.69.140)
REAL = {"74.125.197.134": 2.4, "64.233.178.134": 8.8, "192.178.192.134": 8.8, "142.250.139.134": 9.0,
        "142.250.137.134": 65.6, "142.250.69.140": 65.8, "142.250.69.44": 65.4, "142.250.69.76": 65.8}


def test_prefers_near_fast_ips_and_moves_off_far(paths, tmp_path):
    """v2.22: 가깝고 빠른 IP(가장 빠른 곳의 2배 이내 또는 15ms 이하)만 쓰고, 그 안에서 가장 적게 붙은 곳 · 같으면 더 빠른 곳."""
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: sorted(REAL), doh=lambda h: [],
                               tls_ok=lambda ip, h, p: (True, REAL[ip]), sleep=lambda s: None)
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    assert set(pool.near("a.rtmps.youtube.com", 443)) == {"74.125.197.134", "64.233.178.134", "192.178.192.134", "142.250.139.134"}
    for i in range(1, 51):
        pool.pick(f"live{i}", "a.rtmps.youtube.com", 443)
    cnt = pool.load()
    assert set(cnt) <= set(pool.near("a.rtmps.youtube.com", 443))  # 먼 IP 에는 하나도 안 붙음
    # v2.23: 지역끼리 먼저 고르게 — 지역 A(2.4ms, 1곳) 25 · 지역 B(9ms 대 3곳) 8·8·9
    assert cnt["74.125.197.134"] == 25 and sorted(v for k, v in cnt.items() if k != "74.125.197.134") == [8, 8, 9]
    # 먼 IP 에 붙어 있던 라이브는 자정에 옮김 대상, 가까운 IP 는 붐빌 때만
    for lid, (ip, _t) in pool.reserved.items():
        peers[lid] = ip
    assert pool.should_move("142.250.69.140") and not pool.should_move("74.125.197.134")
    # 중계기용 파일엔 가까운 IP 만 → 다시 접속·옮김도 먼 곳으로 안 감
    pool.write_load()
    d = __import__("json").loads(pool.load_file.read_text())
    assert set(d["hosts"]["a.rtmps.youtube.com:443"]["ips"]) == set(pool.near("a.rtmps.youtube.com", 443))
    s = pool.summary()
    assert s["near"] == 4 and s["ips"] == 8 and s["far_used"] == 0


def test_single_near_ip_takes_all_rather_than_far(paths):
    """가까운 곳이 1곳뿐이면 먼 곳으로 나누지 않고 그 1곳에 (속도가 분산보다 중요 — 사용자 결정)."""
    rt = {"74.125.197.134": 2.3, "142.250.69.44": 65.2}
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: sorted(rt), doh=lambda h: [],
                               tls_ok=lambda ip, h, p: (True, rt[ip]), sleep=lambda s: None)
    for i in range(1, 11):
        assert pool.pick(f"live{i}", "a.rtmps.youtube.com", 443) == "74.125.197.134"
    assert pool.summary()["crowded"] is False  # 할 수 있는 게 없으면 경고 안 함



def test_sites_grouped_by_rtt_and_file_picks_other_healthy_site(paths, tmp_path):
    """v2.23: 왕복 시간이 비슷한 IP 는 같은 지역 (11-14 02:23: 9ms 대 3곳이 같이 막힘, 2.4ms 는 멀쩡).
    막혔을 때 중계기는 다른 지역 · 지금 왕복이 정상인 곳으로."""
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: sorted(REAL), doh=lambda h: [],
                               tls_ok=lambda ip, h, p: (True, REAL[ip]), sleep=lambda s: None)
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    st = pool.sites("a.rtmps.youtube.com", 443)
    assert st["74.125.197.134"] == 0
    assert st["64.233.178.134"] == st["192.178.192.134"] == st["142.250.139.134"] == 1
    assert st["142.250.69.140"] == 2
    lab = pool.site_label("a.rtmps.youtube.com", 443)
    assert lab["74.125.197.134"].startswith("지역 A") and lab["64.233.178.134"].startswith("지역 B (8ms")
    # 02:23 처럼 지역 B 가 18~27ms 로 느려진 순간
    pool.rtt_now_fn = lambda: {"64.233.178.134": 25.2, "192.178.192.134": 18.1, "142.250.139.134": 18.5, "74.125.197.134": 2.6}
    for i in range(1, 51):
        pool.pick(f"live{i}", "a.rtmps.youtube.com", 443)
    for lid, (ip, _t) in pool.reserved.items():
        peers[lid] = ip
    pool.write_load()
    f = pool.load_file
    # 지역 B 의 라이브가 막힘 → 다른 지역(A)의 정상 IP
    assert ytingest.pick_from_file(f, "a.rtmps.youtube.com", 443, "64.233.178.134", avoid_site=True, healthy_only=True) == "74.125.197.134"
    # 지역 A 가 막혔는데 다른 지역(B)도 다 느리면 → 옮기지 않음 (우리 회선 쪽일 수 있음)
    assert ytingest.pick_from_file(f, "a.rtmps.youtube.com", 443, "74.125.197.134", avoid_site=True, healthy_only=True) is None
    # 정상 조건 없이 다른 지역이면 B 중 하나
    assert ytingest.pick_from_file(f, "a.rtmps.youtube.com", 443, "74.125.197.134", avoid_site=True) in {
        "64.233.178.134", "192.178.192.134", "142.250.139.134"}
    s = pool.summary()
    assert s["site_load"] and s["crowded"] is False and s["site_of"]["74.125.197.134"].startswith("지역 A")


def test_trace_on_rtt_spike_twice(paths, monkeypatch):
    """E: 지금 왕복이 기준의 2배 + 8ms 넘게 두 번 → 길 추적 저장 (비교용 다른 지역 IP 도), 30분에 한 번."""
    from livemax import db

    db.init(paths.db)
    traced = []
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: sorted(REAL), doh=lambda h: [],
                               tls_ok=lambda ip, h, p: (True, REAL[ip]), sleep=lambda s: None,
                               tracer=lambda ip: traced.append(ip) or f"hop1 {ip}")
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    pool.rtt_now_fn = lambda: {"64.233.178.134": 25.2, "74.125.197.134": 2.6}
    monkeypatch.setattr(ytingest.threading, "Thread", lambda target, args, **k: type("T", (), {"start": lambda self: target(*args)})())
    assert pool.check_spikes(now=2000.0) == []           # 한 번은 출렁임일 수 있음
    assert pool.check_spikes(now=2015.0) == ["64.233.178.134"]
    assert traced == ["64.233.178.134", "74.125.197.134"]  # 막힌 곳 + 비교용 멀쩡한 지역
    assert pool.check_spikes(now=2030.0) == []           # 30분 안엔 다시 안 함
    with db.session(paths.db) as c:
        row = c.execute("SELECT ip, rtt_base, rtt_now, text FROM traces").fetchone()
    assert row["ip"] == "64.233.178.134" and "비교" in row["text"]
