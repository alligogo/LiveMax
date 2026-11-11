"""서버 박스 '안전기준 N개 | 최소기준 M개 (하루)' (DESIGN 11-D 20 · v2.14)."""

from livemax import capacity


def _cap(cpu, mem=3000.0):
    return capacity.compute({"cpu_target_pct": 50, "mem_reserve_mb": 700, "port_mbit": 1000, "port_use_ratio": 0.5},
                            [5000] * 50, None, 260.0, cpu, mem, 7800.0, live_cpu=[2.8] * 50, running=50, cores=4)


def test_max_guess_matches_screen_rule():
    cap = _cap(36.8)
    n, by = capacity.max_guess(cap, 50)
    assert by == min(cap.options, key=cap.options.get) and n == 50 + max(0, cap.options[by])


def test_day_low_keeps_worst_and_resets_at_day_start():
    low = capacity.DayLow()
    assert low.snapshot() == {}
    low.update(100.0, (74, "cpu"), 1000.0)
    low.update(100.0, (65, "cpu"), 2000.0)  # 새벽 steal 로 나빠짐
    low.update(100.0, (80, "cpu"), 3000.0)  # 다시 좋아져도 최소는 그대로
    assert low.snapshot() == {"guess_low": 65, "guess_low_by": "cpu", "guess_low_at": 2000.0}
    low.update(186500.0, (72, "ram"), 190000.0)  # 다음 날
    assert low.snapshot()["guess_low"] == 72


def test_collector_skips_while_lives_starting(paths, settings):
    from conftest import make_live
    from livemax import config
    from livemax.collector import Collector

    store = config.LiveStore(paths, settings)
    for i in (1, 2):
        store.save(make_live(id=f"live{i}", harbor_port=8100 + i, restart_slot=i, enabled=True))

    class Sup:
        pass

    sup = Sup()
    sup.store = store
    col = Collector(paths, settings, sup)
    col.per_live = {"live1": {"cpu": 2.8}, "live2": {"cpu": 2.8}}
    snap = {"live1": {"state": "running", "kbps_in": 5128}, "live2": {"state": "starting"}}
    col._track_low(1000.0, snap, 30.0, 3000.0, 7800.0, 10.0, None, 1)
    assert col.day_low.snapshot() == {}  # 켜는 중인 라이브가 있으면 세지 않음
    snap["live2"] = {"state": "running", "kbps_in": 5128}
    col._track_low(1015.0, snap, 30.0, 3000.0, 7800.0, 10.0, None, 2)
    assert col.day_low.snapshot()["guess_low"] > 2
