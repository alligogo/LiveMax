"""v2.19: 유튜브 수신 IP 모음 + 가장 적게 붙은 IP 로 붙기."""

import json

import pytest

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


def test_crowded_only_when_far_above_target(paths):
    """v2.28: 목표 몫(가장 빠른 지역 80%) 기준 — 40/10 은 정상, 25/25 는 다른 지역이 목표(10)보다 15 많아 '몰림'.
    지역을 모르면(왕복 시간 없음) 한 지역으로 보고 경고 안 함."""
    rt = {"74.125.197.134": 2.4, "64.233.178.134": 8.8}
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h, p: sorted(rt), doh=lambda h: [],
                               tls_ok=lambda ip, h, p: (True, rt[ip]), sleep=lambda s: None)
    pool.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    peers.update({f"live{i}": "74.125.197.134" if i < 40 else "64.233.178.134" for i in range(50)})
    s = pool.summary()
    assert s["crowded"] is False and s["site_target"] == {"지역 A (2ms대)": 40, "지역 B (8ms대)": 10}
    peers.update({f"live{i}": "74.125.197.134" if i % 2 else "64.233.178.134" for i in range(50)})  # 25 / 25
    s = pool.summary()
    assert s["crowded"] is True and s["crowded_site"] == "지역 B (8ms대)" and (s["site_max"], s["fair"]) == (25, 10)
    pool0, peers0 = _pool(paths, [IPS2])
    pool0.hosts.clear()
    pool0.refresh("a.rtmps.youtube.com", 443, now=1000.0)
    peers0.update({f"live{i}": IPS2[0] for i in range(35)})
    peers0.update({f"live{i}": IPS2[1] for i in range(35, 50)})
    assert pool0.summary()["crowded"] is False
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
    # v2.28: 가장 빠른 지역 80% — 지역 A(2.4ms, 1곳) 40 · 지역 B(9ms 대 3곳) 3·3·4
    assert cnt["74.125.197.134"] == 40 and sorted(v for k, v in cnt.items() if k != "74.125.197.134") == [3, 3, 4]
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


def test_in_use_ip_is_kept_and_near_shortage_recollects(paths):
    """v2.25 (11-15): 8.7ms 로 라이브를 잘 보내던 IP 가 'DNS 에 48시간 안 보임' 으로 모음에서 빠져 가까운 IP 가 4곳 → 2곳.
    붙어서 쓰는 중인 IP 는 안 빼고, 가까운 IP 가 4곳 아래면 (먼 IP 가 숫자를 채워도) 다시 모은다."""
    h = "a.rtmps.youtube.com"
    rt = {"74.125.197.134": 2.4, "64.233.178.134": 8.8, "192.178.192.134": 8.7, "142.250.139.134": 8.9,
          "142.250.69.44": 67.0, "142.250.69.140": 65.8, "142.250.69.76": 65.5}
    dns, doh, peers = [sorted(rt)], [[]], {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h_, p: list(dns[0]), doh=lambda h_: list(doh[0]),
                               tls_ok=lambda ip, h_, p: (True, rt[ip]), sleep=lambda s: None)
    pool.refresh(h, 443, now=1000.0)
    assert len(pool.near(h, 443)) == 4
    peers["live1"] = "192.178.192.134"  # 이 IP 로 송출 중
    dns[0] = ["74.125.197.134", "64.233.178.134", "142.250.69.44", "142.250.69.140", "142.250.69.76"]  # DNS 가 2곳을 더는 안 알려 줌
    pool.refresh(h, 443, now=1000.0 + ytingest.POOL_TTL_S + 10)
    assert "192.178.192.134" in pool.healthy(h, 443)       # 쓰는 중 → 남김
    assert "142.250.139.134" not in pool.healthy(h, 443)   # 안 쓰고 48시간 안 보임 → 뺌
    assert len(pool.healthy(h, 443)) == 6 and len(pool.near(h, 443)) == 3
    doh[0] = ["142.250.139.134"]  # 공개 DNS 는 아직 알려 줌
    pool.ensure(h, 443)           # 전체는 6곳이라 예전엔 안 모았다 — 가까운 곳이 3곳이라 모은다
    assert len(pool.near(h, 443)) == 4 and "142.250.139.134" in pool.near(h, 443)
    peers["live2"] = "9.9.9.9"    # 모음 밖 IP 에 붙은 라이브는 '먼 곳(자정에 옮김)' 으로 세지 않는다 (옮김 대상이 아님)
    assert pool.summary()["far_used"] == 0


def test_base_rtt_is_24h_minimum_and_rtmp_check(paths):
    """v2.28: '평소 왕복' = 24시간 안에 잰 값 중 가장 작은 값 (11-17: 막힌 순간 65ms 가 평소가 돼 9ms IP 가 '지역 C' 로).
    RTMP 첫 인사에 답이 없는 IP(구글의 다른 서버)는 안 씀 — 단 모두 실패하면 확인 방법 문제로 보고 무시."""
    h = "a.rtmps.youtube.com"
    meas = {"74.125.197.134": [(2.3, True)], "64.233.178.134": [(8.7, True)], "142.250.72.12": [(10.3, False)]}
    step = [0]
    peers: dict = {}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h_, p: sorted(meas), doh=lambda h_: [], sleep=lambda s: None,
                               tls_ok=lambda ip, h_, p: (True, meas[ip][min(step[0], len(meas[ip]) - 1)][0],
                                                         meas[ip][min(step[0], len(meas[ip]) - 1)][1]))
    pool.refresh(h, 443, now=1000.0)
    assert "142.250.72.12" not in pool.healthy(h, 443) and len(pool.healthy(h, 443)) == 2
    meas["64.233.178.134"].append((65.1, True))  # 한 시간 뒤 막힌 순간에 잼
    step[0] = 1
    pool.refresh(h, 443, now=1000.0 + 3600)
    assert pool.rtt_of(h, 443)["64.233.178.134"] == 8.7  # 평소는 그대로
    assert set(pool.near(h, 443)) == {"74.125.197.134", "64.233.178.134"}
    pool.refresh(h, 443, now=1000.0 + 3600 + ytingest.RTT_HIST_S + 10)  # 24시간 넘게 지나면 옛 값은 빠짐
    assert pool.rtt_of(h, 443)["64.233.178.134"] == 65.1
    # 모두 RTMP 실패 → 확인 방법 문제로 보고 TLS 결과로
    bad = {"1.1.1.1": 2.0, "2.2.2.2": 9.0}
    p2 = ytingest.IngestPool(paths, lambda: {}, resolve=lambda h_, p: sorted(bad), doh=lambda h_: [], sleep=lambda s: None,
                             tls_ok=lambda ip, h_, p: (True, bad[ip], False))
    p2.hosts.clear()
    p2.refresh("b", 443, now=1000.0)
    assert p2.healthy("b", 443) == ["1.1.1.1", "2.2.2.2"]


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg 없음")
def test_rtmp_hello_against_real_rtmp_server():
    """RTMP 첫 인사 확인이 진짜 RTMP 서버(ffmpeg -listen)에선 성공, RTMP 가 아닌 서버에선 실패."""
    import socket as so
    import subprocess as sp
    import time as tm

    s = so.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = sp.Popen(["ffmpeg", "-v", "quiet", "-listen", "1", "-i", f"rtmp://127.0.0.1:{port}/live/x", "-f", "null", "-"])
    try:
        for _ in range(50):
            try:
                c = so.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                tm.sleep(0.1)
        with c:
            assert ytingest.rtmp_hello(c, timeout=3) is True
    finally:
        srv.kill()
    ls = so.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    with so.create_connection(ls.getsockname(), timeout=1) as c2:
        conn, _ = ls.accept()
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        conn.close()
        assert ytingest.rtmp_hello(c2, timeout=1) is False
    ls.close()


def test_primary_share_moves_and_rebalance_after_evacuation(paths, tmp_path):
    """v2.28 가장 빠른 지역 80%: 고르기·자정 옮김·중계기 파일 고르기 모두 목표 몫 기준.
    대피 뒤 되돌리기: 모자란 지역이 30분 넘게 정상일 때만, 가장 붐비는 IP 의 라이브부터 하나씩. 라이브 없는 지역은 접속 시간을 직접 잼."""
    h = "a.rtmps.youtube.com"
    rt = {"74.125.197.134": 2.4, "64.233.178.134": 8.8, "192.178.192.134": 8.6}
    peers: dict = {}
    now_rtt: dict = {}
    probe = {"74.125.197.134": 2.5}
    pool = ytingest.IngestPool(paths, lambda: peers, resolve=lambda h_, p: sorted(rt), doh=lambda h_: [], sleep=lambda s: None,
                               tls_ok=lambda ip, h_, p: (True, rt[ip], True), tcp_rtt=lambda ip, p: probe.get(ip))
    pool.rtt_now_fn = lambda: now_rtt
    pool.refresh(h, 443, now=1000.0)
    # 지역 A 가 막혀 40개가 모두 B 로 대피한 상태: A 0 · B 50
    peers.update({f"live{i}": ("64.233.178.134" if i % 2 else "192.178.192.134") for i in range(50)})
    now_rtt.update({"64.233.178.134": 9.0, "192.178.192.134": 8.9})
    assert pool.should_move("64.233.178.134") and not pool.should_move("74.125.197.134")
    t = 5000.0
    pool.track_sites(now=t)  # A 에는 라이브가 없어 접속 시간을 잼 (2.5ms — 정상)
    assert pool.probe["74.125.197.134"][1] == 2.5
    assert pool.rebalance_pick(now=t + 60) is None              # 아직 30분 안 됨
    pool.track_sites(now=t + ytingest.REBAL_OK_S)
    lid = pool.rebalance_pick(now=t + ytingest.REBAL_OK_S + 1)
    assert lid is not None and peers[lid] in ("64.233.178.134", "192.178.192.134")
    # 중계기 쪽 파일 고르기도 몫 기준 → A
    pool.load_file = tmp_path / "load.json"
    pool.write_load(now=t + ytingest.REBAL_OK_S + 2)
    d = json.loads(pool.load_file.read_text())["hosts"][f"{h}:443"]
    assert d["rtt_now"]["74.125.197.134"] == 2.5 and d["weight"] == {"0": 0.8, "1": 0.2}
    assert ytingest.pick_from_file(pool.load_file, h, 443, peers[lid]) == "74.125.197.134"
    # A 가 다시 느려지면(접속 4배) 정상 시각이 지워져 되돌리지 않음
    probe["74.125.197.134"] = 20.0
    pool.track_sites(now=t + ytingest.REBAL_OK_S + ytingest.PROBE_S + 1)
    assert pool.rebalance_pick(now=t + ytingest.REBAL_OK_S + ytingest.PROBE_S + 2) is None
    # 40 / 10 (B 안에서 5·5) 이면 옮길 것 없음
    peers.clear()
    peers.update({f"live{i}": "74.125.197.134" if i < 40 else ("64.233.178.134" if i % 2 else "192.178.192.134") for i in range(50)})
    assert not any(pool.should_move(ip) for ip in rt)
