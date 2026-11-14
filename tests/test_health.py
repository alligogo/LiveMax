"""v2.19 건강검진: 시청자 경험 기준 판정 · 원인 · 우리가 한 일 제외 · 10일 보관."""

import time

from livemax import db, health


def _ins(c, ts, lid, kind, **kw):
    c.execute("INSERT INTO incidents(ts, live_id, kind, sec, max_delay, peer, where_, note) VALUES (?,?,?,?,?,?,?,?)",
              (ts, lid, kind, kw.get("sec"), kw.get("max_delay"), kw.get("peer"), kw.get("where"), None))


def _metrics(c, t0, t1, lives=50, cpu=33.0):
    t = t0
    while t < t1:
        c.execute("INSERT INTO metrics_system(ts, cpu_pct, steal_pct, iowait_pct, mem_used_mb, mem_total_mb, disk_used_gb,"
                  " disk_total_gb, net_tx_mbps, net_rx_mbps, month_tx_gb, lives_running) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (t, cpu, 0.4, 0, 3100, 7800, 10.3, 77, 260, 5, 0, lives))
        t += 900


def test_verdict_causes_and_ours(paths, settings):
    end = time.time()
    with db.session(paths.db) as c:
        _metrics(c, end - 86400, end)
        t = end - 6 * 3600
        # 같은 IP 에 붙은 3개가 같은 순간 길게 밀림 → ✗, 원인 '유튜브 IP'
        for lid in ("live32", "live35", "live36"):
            _ins(c, t, lid, "slow", sec=20, max_delay=15, peer="142.250.69.140", where="network")
        # 여러 IP 의 3개가 같은 순간 → '회선'
        t2 = end - 3 * 3600
        for lid, ip in (("live1", "1.1.1.1"), ("live2", "2.2.2.2"), ("live3", "3.3.3.3")):
            _ins(c, t2, lid, "slow", sec=8, max_delay=6, peer=ip, where="network")
        _ins(c, end - 2 * 3600, "live9", "slow", sec=5, max_delay=3, peer="x")  # 짧음 → ✓ 내부 처리
        _ins(c, end - 3600, "live7", "no_audio", sec=8)  # 우리 프로그램 → ✗
        # 업데이트 직후 5분 안의 밀림 → 우리가 한 일 (판정에서 뺌)
        _ins(c, end - 1800, None, "boot")
        _ins(c, end - 1700, "live4", "slow", sec=30, max_delay=20, peer="y")
        rep = health.compute(c, settings, end, "v-test", names={"live32": "LIVE 32 (MilSim)"})
    assert rep["verdict"] == "bad" and rep["counts"]["bad"] == 4 and rep["counts"]["warn"] == 3
    assert rep["counts"]["minor"] == 1 and rep["counts"]["ours"] == 1 and rep["counts"]["ours_program"] == 1
    assert rep["causes"]["유튜브 IP 142.250.69.140 경로 혼잡"] == 3 and rep["causes"]["회선 (여러 지역 동시)"] == 3
    assert any(e["name"] == "LIVE 32 (MilSim)" for e in rep["events"])
    assert 0.999 < rep["avail"] < 1 and rep["server"]["cpu_avg"] == 33.0
    title, body = health.message(rep)
    assert "✗" in title and "정상 46/50" in title and "가장 길게" in body and "소리 없음" in body
    assert (rep["lives_total"], rep["lives_ok"], rep["lives_bad"]) == (50, 46, 4) and rep["longest_s"] == 15.0


def test_daily_restart_window_and_thresholds(paths, settings):
    end = time.time()
    with db.session(paths.db) as c:
        _metrics(c, end - 86400, end)
        _ins(c, end - 5000, "live1", "planned_restart")
        _ins(c, end - 4950, "live1", "reconnect", sec=4)  # 재시작 50초 뒤 → 우리가 한 일
        _ins(c, end - 3000, "live2", "slow", sec=10, max_delay=6)  # 5~8초 → △
        rep = health.compute(c, settings, end)
    assert rep["verdict"] == "warn" and rep["counts"]["ours"] == 1
    settings["health"] = dict(settings.get("health") or {}, warn_s=7)
    with db.session(paths.db) as c:
        rep2 = health.compute(c, settings, end)
    assert rep2["verdict"] == "ok"  # 기준을 7초로 바꾸면 6초 밀림은 ✓


def test_store_keeps_ten_days(paths, settings):
    with db.session(paths.db) as c:
        for d in range(1, 14):
            rep = health.compute(c, settings, time.time())
            rep["day"] = f"2026-11-{d:02d}"
            health.store(c, rep)
        ds = health.days(c)
    assert len(ds) == 10 and ds[0]["day"] == "2026-11-04" and ds[-1]["day"] == "2026-11-13"


def test_health_api_and_settings(client):
    from test_api import NEW, ORIGIN, login

    login(client, client.initial_pw)
    client.post("/api/password", json={"current": client.initial_pw, "new": NEW}, headers=ORIGIN)
    d = client.get("/api/health").json()
    assert d["days"] == [] and d["now"]["day"] == "지금" and d["settings"]["time_kst"] == "07:00"
    r = client.put("/api/system/settings", headers=ORIGIN, json={"health_warn_s": 9, "health_crit_s": 8})
    assert r.status_code == 400  # 영향 가능 기준은 시청자 영향 기준보다 작아야
    r = client.put("/api/system/settings", headers=ORIGIN,
                   json={"health_time_kst": "06:30", "health_notify_mode": "problem", "health_warn_s": 4, "health_crit_s": 9})
    assert r.status_code == 200
    s = client.get("/api/health").json()["settings"]
    assert (s["time_kst"], s["notify_mode"], s["warn_s"], s["crit_s"]) == ("06:30", "problem", 4, 9)
    n = client.get("/api/system").json()["notify"]
    assert n["health"]["time_kst"] == "06:30" and any(c["key"] == "health" for c in n["catalog"])



def test_same_site_is_path_congestion_not_line(paths, settings):
    """v2.23 D: 11-14 02:23 — IP 3곳(9ms 대 같은 지역)의 22개가 같이 밀림. '여러 IP 동시 = 회선' 이 아니라 '지역 B 경로 혼잡'.
    정상 N/50 · 가장 길게 · 사건 횟수."""
    end = time.time()
    site_of = {"64.233.178.134": "지역 B (8ms대)", "192.178.192.134": "지역 B (8ms대)", "142.250.139.134": "지역 B (8ms대)",
               "74.125.197.134": "지역 A (2ms대)"}
    with db.session(paths.db) as c:
        _metrics(c, end - 86400, end)
        t = end - 5 * 3600
        ips = ["64.233.178.134", "192.178.192.134", "142.250.139.134"]
        for i in range(22):
            _ins(c, t + i, f"live{i}", "slow", sec=20, max_delay=10 + i, peer=ips[i % 3], where="network")
        _ins(c, t + 30, "live40", "auto_move", sec=0.4, peer="74.125.197.134")
        rep = health.compute(c, settings, end, ingest={"site_of": site_of})
    assert rep["causes"] == {"유튜브 지역 B (8ms대) 경로 혼잡": 22}
    assert (rep["lives_ok"], rep["lives_total"], rep["longest_s"]) == (28, 50, 31.0)
    assert len(rep["episodes"]) == 1 and rep["episodes"][0]["n"] == 22 and rep["server"]["auto_moves"] == 1
    title, body = health.message(rep)
    assert title.endswith("정상 28/50") and "사건 1번" in body and "지역 B" in body
