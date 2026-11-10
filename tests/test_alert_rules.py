"""밀림·재연결 알림 기준 (DESIGN 11-D 11) + 로그에 사람이 읽는 이름 (DESIGN 11-D 10)."""

import time

from conftest import make_live
from livemax import config, db, supervisor


def _sup(paths, settings, n=4):
    settings["server_name"] = "OVH1"
    sup = supervisor.Supervisor(paths, settings)
    store = config.LiveStore(paths, settings)
    for i in range(1, n + 1):
        store.save(make_live(id=f"live{i}", name=f"LIVE {i}", harbor_port=8100 + i, restart_slot=i))
    sent = []
    sup.notify = lambda *a, **k: sent.append(a) or True
    return sup, sent


def _events(paths):
    with db.session(paths.db) as c:
        return [(r["level"], r["message"]) for r in c.execute("SELECT level, message FROM events ORDER BY id")]


def test_quiet_reconnect_not_counted_or_alerted(paths, settings):
    sup, sent = _sup(paths, settings)
    p = sup.proc("live1")
    p._relay_event({"event": "reconnect", "gap": 0.7})
    snap = p.snapshot()
    assert snap["restarts_hour"] == 0 and snap["restarts"][-1]["reason"].startswith(supervisor.QUIET_RECONNECT)
    assert _events(paths) == []  # 알림 박스에 안 뜸
    p._relay_event({"event": "reconnect", "gap": 7.3})  # 5초 넘게 걸리면 알림
    assert p.snapshot()["restarts_hour"] == 1 and "7.3초" in _events(paths)[-1][1]


def test_short_slow_is_silent_long_or_repeated_alerts(paths, settings):
    sup, sent = _sup(paths, settings)
    p = sup.proc("live1")
    p._relay_event({"event": "slow_end", "dur": 8, "max_delay": 3})
    p._relay_event({"event": "slow_end", "dur": 6, "max_delay": 2})
    assert _events(paths) == []
    p._relay_event({"event": "slow_end", "dur": 7, "max_delay": 2})  # 1시간에 3번째 → 알림 한 번
    assert "1시간에 밀림 3번" in _events(paths)[-1][1]
    p._relay_event({"event": "slow_end", "dur": 7, "max_delay": 2})  # 같은 시간 안엔 다시 안 울림
    assert len(_events(paths)) == 1
    q = sup.proc("live2")
    q._relay_event({"event": "slow_end", "dur": 31, "max_delay": 19})  # 30초 넘게 → 알림
    assert "유튜브 전송 밀림 31초" in _events(paths)[-1][1]
    assert sent == []  # 라이브 하나의 밀림은 휴대폰으로 안 보냄


def test_mass_slow_pushes_phone_once(paths, settings):
    sup, sent = _sup(paths, settings, n=4)
    for lid in ("live1", "live2"):
        sup.proc(lid)._relay_event({"event": "slow_start", "delay": 3})
    assert sent == []
    sup.proc("live3")._relay_event({"event": "slow_start", "delay": 3})
    assert len(sent) == 1 and sent[0][0] == "down" and "3개" in sent[0][2]
    sup.proc("live4")._relay_event({"event": "slow_start", "delay": 3})
    assert len(sent) == 1  # 10분 안엔 한 번만
    assert any(lv == "error" and "동시에" in m for lv, m in _events(paths))


def test_mass_slow_ignores_just_started(paths, settings):
    sup, sent = _sup(paths, settings, n=3)
    for lid in ("live1", "live2", "live3"):
        p = sup.proc(lid)
        p.set(ffmpeg_started_at=time.time())  # 막 켜진 라이브 (업데이트·부팅 직후)
        p._relay_event({"event": "slow_start", "delay": 3})
    assert sent == []


def test_blips_counted_for_card(paths, settings):
    sup, _ = _sup(paths, settings)
    p = sup.proc("live1")
    for d in (0.2, 1.4, 2.1, 0.3, 0.1, 1.1, 0.2):
        p._relay_event({"event": "stats", "delay": d, "kbps_in": 5000, "kbps_out": 5000})
    assert p.snapshot()["blips_day"] == {"n": 2, "max": 2.1}


def test_live_log_uses_readable_name(paths, settings):
    import logging
    lg = logging.getLogger("livemax.live.live2")  # 앞 테스트가 남긴 (다른 임시 폴더의) 핸들러 정리
    for h in list(lg.handlers):
        lg.removeHandler(h)
    sup, _ = _sup(paths, settings)
    p = sup.proc("live2")
    p.log.warning("테스트 줄")
    for h in p.log.handlers:
        h.flush()
    line = (paths.logs / "live-live2.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "[OVH1 · LIVE 2 · live2] 테스트 줄" in line


def test_day_start_is_last_daily_restart_base(settings):
    from datetime import datetime, timezone
    settings["restart"]["base_utc"] = "15:00"  # 한국 자정
    t = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc).timestamp()   # 한국 12:00 → 그날 자정(전날 15:00 UTC)
    assert config.day_start(settings, t) == datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc).timestamp()
    t2 = datetime(2026, 11, 7, 15, 30, tzinfo=timezone.utc).timestamp()  # 한국 00:30 → 30분 전 자정
    assert config.day_start(settings, t2) == datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc).timestamp()


def test_overview_has_day_start_and_media_totals(client):
    from test_api import _ready
    _ready(client)
    ov = client.get("/api/overview").json()
    assert ov["day_start"] <= ov["server_time"] < ov["day_start"] + 86400
    assert set(ov["media"]) == {"video", "audio"} and "n" in ov["media"]["video"]


def test_swap_counts_as_used_memory_in_capacity(client):
    """스왑으로 옮겨진 몫도 '쓰는 메모리'로 쳐서 남은 여유·예상 최대가 부풀려 보이지 않게."""
    from test_api import _ready
    _ready(client)
    base = {"cpu_pct": 10.0, "mem_used_mb": 4600.0, "mem_total_mb": 7751.0, "disk_used_gb": 9, "disk_total_gb": 77,
            "net_tx_mbps": 200.0, "month_tx_bytes": 0}
    client.core.collector.latest = {**base, "swap_used_mb": 0.0}
    no_swap = client.get("/api/overview").json()
    import time as _t
    _t.sleep(4.1)  # overview 4초 캐시
    client.core.collector.latest = {**base, "swap_used_mb": 1400.0}
    with_swap = client.get("/api/overview").json()
    assert with_swap["system"]["swap_used_mb"] == 1400.0
    assert with_swap["capacity"]["options"]["ram"] < no_swap["capacity"]["options"]["ram"]


def test_sysmetrics_swap_readers():
    from livemax import sysmetrics
    used, total = sysmetrics.read_swap()
    assert used >= 0 and total >= 0 and used <= total + 1
    assert isinstance(sysmetrics.read_swap_io(), int)


def test_boot_waits_when_memory_tight(paths, settings, monkeypatch):
    """업데이트·부팅 순차 시작: 메모리가 빠듯하면 다음 라이브를 조금 미루고, 여유가 있으면 바로 넘어간다."""
    sup, _ = _sup(paths, settings, n=1)
    waits = []
    monkeypatch.setattr(sup.stop_event, "wait", lambda t: waits.append(t) or False)
    monkeypatch.setattr(supervisor.sysmetrics, "read_swap_io", lambda: 0)
    monkeypatch.setattr(supervisor.sysmetrics, "read_mem", lambda: (2000.0, 7751.0))   # 여유 많음
    assert sup._wait_for_memory(0, 10) is False and waits == []
    seq = iter([(7300.0, 7751.0), (7300.0, 7751.0), (5000.0, 7751.0)])               # 빠듯 → 빠듯 → 풀림
    monkeypatch.setattr(supervisor.sysmetrics, "read_mem", lambda: next(seq))
    assert sup._wait_for_memory(0, 10) is False and waits == [5, 5]
    waits.clear()
    monkeypatch.setattr(supervisor.sysmetrics, "read_mem", lambda: (7600.0, 7751.0))   # 계속 빠듯 → 최대 30초만
    assert sup._wait_for_memory(0, 10) is False and sum(waits) == 30


def test_big_backlog_alerts_even_when_short(paths, settings):
    """실제 사건(LIVE 26): 18초 동안 최대 17.8초 밀림 + 3초 만에 재접속 — 예전 기준(30초·재접속 5초)으로는 조용히 넘어갔다."""
    sup, _sent = _sup(paths, settings)
    p = sup.proc("live1")
    p._relay_event({"event": "slow_end", "dur": 18, "max_delay": 17.8, "kbps_out": 433, "kbps_in": 5149})
    ev = _events(paths)
    assert len(ev) == 1 and ev[0][0] == "warn" and "17.8초" in ev[0][1]
    p._relay_event({"event": "slow_end", "dur": 12, "max_delay": 4})  # 조금 밀린 것은 여전히 조용히
    assert len(_events(paths)) == 1


def test_no_audio_restarts_only_that_live_and_respects_limit(paths, settings):
    """중계기가 '소리 없음'을 알리면 그 라이브의 ffmpeg 만 다시 띄우고 알림. 1시간 한도를 넘으면 더 안 건드리고 '확인 필요' 알림."""
    sup, sent = _sup(paths, settings)
    p = sup.proc("live1")
    calls = []
    p.restart = lambda reason, audio_too=False: (calls.append(reason), p.restarts.append((time.time(), reason)))
    p._relay_event({"event": "no_audio", "sec": 8})
    assert calls == []  # 송출 중이 아니면 아무것도 안 함
    p.desired = True
    p.set(state="running")
    p._relay_event({"event": "no_audio", "sec": 8})
    assert calls == ["소리 없음"] and sent[-1][0] == "restart" and "소리" in _events(paths)[-1][1]
    p._relay_event({"event": "no_input", "sec": 10})  # 10초: 기록만
    assert calls == ["소리 없음"] and "10초째" in _events(paths)[-1][1]
    p._relay_event({"event": "no_input", "sec": 30})  # 30초: 다시 띄움
    assert calls == ["소리 없음", "입력 없음"]
    for _ in range(int(settings["watchdog"]["max_restarts_per_hour"])):
        p.restarts.append((time.time(), "소리 없음"))
    p._relay_event({"event": "no_audio", "sec": 8})
    assert len(calls) == 2 and sent[-1][0] == "down" and "한도" in _events(paths)[-1][1]


def test_peak_tracker_keeps_daily_max_and_resets(monkeypatch):
    """CPU·메모리 오늘 최고값: 1초 표본 중 최고와 그 시각만 기억, 하루 기준(day_start)이 바뀌면 처음부터."""
    from livemax import sysmetrics as sm

    cpu = iter([sm.CpuTimes(1000, 900, 0, 0), sm.CpuTimes(1100, 960, 0, 0), sm.CpuTimes(1200, 1050, 0, 0),
                sm.CpuTimes(1300, 1140, 0, 0), sm.CpuTimes(1400, 1210, 0, 0)])
    mem = iter([(2000, 8000), (2600, 8000), (2300, 8000), (2100, 8000), (2200, 8000)])
    monkeypatch.setattr(sm, "read_cpu", lambda: next(cpu))
    monkeypatch.setattr(sm, "read_mem", lambda: next(mem))
    monkeypatch.setattr(sm, "read_swap", lambda: (100, 2000))
    day = {"v": 1.0}
    pk = sm.PeakTracker(lambda now: day["v"])
    for t in (10.0, 11.0, 12.0):
        pk.sample(t)
    s = pk.snapshot()
    assert s["cpu_peak_pct"] == 40.0 and s["cpu_peak_at"] == 11.0  # 100 중 60 이 쉼 → 40%
    assert s["mem_peak_mb"] == 2700 and s["mem_peak_at"] == 11.0   # 스왑 100MB 포함
    day["v"] = 2.0  # 일일 재시작 시각을 지남 → 새 하루
    pk.sample(13.0)
    assert pk.snapshot()["cpu_peak_pct"] == 10.0 and pk.snapshot()["mem_peak_mb"] == 2200
