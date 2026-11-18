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
        # 두 곳(지역)에서 각각 3개씩 같은 순간 → '회선' (v2.24: 한 곳에 1개씩 섞인 건 회선 아님)
        t2 = end - 3 * 3600
        for lid, ip in (("live1", "1.1.1.1"), ("live2", "1.1.1.1"), ("live3", "1.1.1.1"),
                        ("live11", "2.2.2.2"), ("live12", "2.2.2.2"), ("live13", "2.2.2.2")):
            _ins(c, t2, lid, "slow", sec=8, max_delay=6, peer=ip, where="network")
        _ins(c, end - 2 * 3600, "live9", "slow", sec=5, max_delay=3, peer="x")  # 짧음 → ✓ 내부 처리
        _ins(c, end - 3600, "live7", "no_audio", sec=8)  # 우리 프로그램 → ✗
        # 업데이트 직후 5분 안의 밀림 → 우리가 한 일 (판정에서 뺌)
        _ins(c, end - 1800, None, "boot")
        _ins(c, end - 1700, "live4", "slow", sec=30, max_delay=20, peer="y")
        rep = health.compute(c, settings, end, "v-test", names={"live32": "LIVE 32 (MilSim)"})
    assert rep["verdict"] == "bad" and rep["counts"]["bad"] == 4 and rep["counts"]["warn"] == 6
    assert rep["counts"]["minor"] == 1 and rep["counts"]["ours"] == 1 and rep["counts"]["ours_program"] == 1
    assert rep["causes"]["유튜브 IP 142.250.69.140 경로 혼잡"] == 3 and rep["causes"]["회선 (여러 지역 동시)"] == 6
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
        _ins(c, t + 10, "live41", "slow", sec=6, max_delay=3, peer="74.125.197.134")  # 지역 A 작은 밀림 1개 섞임 → 그래도 지역 B 문제
        rep = health.compute(c, settings, end, ingest={"site_of": site_of})
    assert rep["causes"] == {"유튜브 지역 B (8ms대) 경로 혼잡": 22}
    assert (rep["lives_ok"], rep["lives_total"], rep["longest_s"]) == (28, 50, 31.0)
    assert len(rep["episodes"]) == 1 and rep["episodes"][0]["n"] == 22 and rep["server"]["auto_moves"] == 1
    title, body = health.message(rep)
    assert title.endswith("정상 28/50") and "사건 1번" in body and "지역 B" in body


def test_handoff_gap_is_recorded_not_judged_and_no_video_is_ours(paths, settings):
    """v2.25: 재시작 공백(유튜브 연결은 유지, 약 1.4초)은 '서버 상태' 숫자로만 — 판정·사건 목록엔 안 넣는다.
    재생목록이 끝나 소리만 나간 '영상 없음' 은 우리 프로그램 문제 ✗."""
    end = time.time()
    with db.session(paths.db) as c:
        _metrics(c, end - 86400, end)
        for i, g in enumerate((1.2, 1.4, 1.6)):
            _ins(c, end - 7200 + i * 120, f"live{i}", "handoff", sec=g)
        rep = health.compute(c, settings, end)
        assert rep["verdict"] == "ok" and rep["events"] == []
        s = rep["server"]
        assert (s["handoff_n"], s["handoff_avg"], s["handoff_max"]) == (3, 1.4, 1.6)
        _ins(c, end - 3600, "live9", "no_video", sec=8)
        rep = health.compute(c, settings, end)
    assert rep["verdict"] == "bad" and rep["causes"] == {"우리 프로그램": 1}


def test_rescued_slow_is_marked_auto_evacuated(paths, settings):
    """v2.28: 밀림이 '막히기 전에 옮김'(6초)으로 끝났으면 '자동 대피' 표시 — 장치가 막아 낸 것과 진짜 문제를 구분."""
    end = time.time()
    with db.session(paths.db) as c:
        _metrics(c, end - 86400, end)
        t = end - 3600
        _ins(c, t, "live1", "slow", sec=6, max_delay=6.4, peer="64.233.178.134", where="network")
        _ins(c, t + 6, "live1", "auto_move", sec=0.2, peer="64.233.178.134")
        _ins(c, t + 100, "live2", "slow", sec=7, max_delay=5.5, peer="64.233.178.134", where="network")  # 스스로 풀림
        rep = health.compute(c, settings, end)
    ev = {e["live"]: e for e in rep["events"]}
    assert ev["live1"]["rescued"] is True and ev["live2"]["rescued"] is False
    assert rep["counts"]["warn"] == 2 and rep["counts"]["rescued"] == 1
    assert "(자동 대피 1)" in health.message(rep)[1]


def test_v229_short_reconnects_counted_apart_and_summary_line(paths, settings):
    """v2.29 (11-18 06:23): 유튜브 A 서버가 연결을 하나씩 닫음 — 0.2초 재연결이 '짧은 밀림' 으로 셌던 것을 나눠 셈.
    밀림 없이 여러 개가 다시 붙기만 했으면 원인은 '서버가 연결을 끊음'(길 혼잡 아님). 맨 위 한 줄 요약."""
    end = time.time()
    site_of = {"74.125.197.134": "지역 A (2ms대)"}
    with db.session(paths.db) as c:
        _metrics(c, end - 86400, end)
        t = end - 6 * 3600
        for i in range(30):
            _ins(c, t + i * 20, f"live{i}", "reconnect", sec=0.25, peer="74.125.197.134")
        _ins(c, t + 5, "live40", "reconnect", sec=1.7, peer="74.125.197.134")
        _ins(c, t + 330, "live41", "reconnect", sec=2.4, peer="74.125.197.134")
        _ins(c, t + 7200, "live42", "slow", sec=3, max_delay=2.5, peer="64.233.178.134")
        rep = health.compute(c, settings, end, ingest={"site_of": site_of})
    s = rep["server"]
    assert (s["minor_reconnect"], s["minor_slow"]) == (30, 1)
    assert rep["causes"] == {"유튜브 지역 A (2ms대) 서버가 연결을 끊음": 2}
    assert rep["summary"] == ("시청자 영향 없음 · 작은 끊김 2건(최대 2.4초) · 원인: 유튜브 지역 A (2ms대) 서버가 연결을 끊음"
                              " · 짧은 재연결 30번(1초 미만)")
    assert health.message(rep)[1].startswith(rep["summary"])
    with db.session(paths.db) as c:
        old = dict(rep)
        old.pop("summary")
        old["day"] = "2026-11-01"
        health.store(c, old)
        assert health.days(c)[0]["summary"] == rep["summary"]  # 예전에 저장된 날도 한 줄 요약
