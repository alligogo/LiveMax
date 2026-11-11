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
    t = 100000.0
    old = t - 3600
    snap = {"live1": {"state": "running", "kbps_in": 5128, "ffmpeg_started_at": old}, "live2": {"state": "starting"}}
    col._track_low(t, snap, 30.0, 3000.0, 7800.0, 10.0, None, 1)
    assert col.day_low.snapshot() == {}  # 켜는 중인 라이브가 있으면 세지 않음
    snap["live2"] = {"state": "running", "kbps_in": 5128, "ffmpeg_started_at": t - 60}
    for i in range(30):
        col._track_low(t + i * 15, snap, 30.0, 3000.0, 7800.0, 10.0, None, 2)
    assert col.day_low.snapshot() == {}  # 켠 지 5분 안 된 라이브가 있으면 세지 않음
    snap["live2"]["ffmpeg_started_at"] = old
    t += 1000
    for i in range(19):  # 5분이 다 차기 전엔 안 셈
        col._track_low(t + i * 15, snap, 30.0, 3000.0, 7800.0, 10.0, None, 2)
    assert col.day_low.snapshot() == {}
    for i in range(19, 40):
        col._track_low(t + i * 15, snap, 30.0, 3000.0, 7800.0, 10.0, None, 2)
    calm = col.day_low.snapshot()["guess_low"]
    # 1분짜리 피크(우분투 보안 점검: 32% → 57%)는 5분 평균에 묻혀 최소기준이 '더 넣을 수 없음'까지 안 떨어진다
    for i in range(40, 44):
        col._track_low(t + i * 15, snap, 57.0, 3000.0, 7800.0, 10.0, None, 2)
    for i in range(44, 70):
        col._track_low(t + i * 15, snap, 30.0, 3000.0, 7800.0, 10.0, None, 2)
    spike = col.day_low.snapshot()["guess_low"]
    assert 2 < spike <= calm and spike > calm * 0.6
    # 오래가는 나쁜 조건(5분 넘게 57%)은 잡힌다
    for i in range(70, 100):
        col._track_low(t + i * 15, snap, 57.0, 3000.0, 7800.0, 10.0, None, 2)
    assert col.day_low.snapshot()["guess_low"] == 2  # 안전선(50%) 넘음 → 더 넣을 수 없음


def test_add_by_profile_1080_vs_4k():
    """v2.17: '추가 가능 1080p / 4K'. 50개 실측(11-11)에서 뽑은 몫으로 빈 서버를 계산하면 운영 기준과 맞아야 한다:
    1080p 는 CPU 로 막히고(약 75개 — 안전선 50% 기준), 4K 15M 은 30개 안팎 (DESIGN 11-G 운영 목표 25~28)."""
    cfg = {"cpu_target_pct": 50, "mem_reserve_mb": 700, "port_mbit": 1000, "port_use_ratio": 0.5, "mem_per_live_mb": 80}
    a_core = 8.0 * 4 / 50                       # 음악 몫: 서버의 8.0% ÷ 50개 (코어 1개 기준 %)
    v_per = (11.9 + 11.4) * 4 / 50 / 5.27       # 영상 몫: (중계기 + ffmpeg) ÷ 50개 ÷ 5.27Mbps
    empty = capacity.compute(cfg, [], None, 0.0, 2.0, 600.0, 7800.0, live_cpu=[], running=0, cores=4)
    n1080, by1080 = capacity.add_by_profile(cfg, empty, 5000, 128, 0, a_core, v_per)
    n4k, by4k = capacity.add_by_profile(cfg, empty, 14000, 200, 20, a_core, v_per)
    assert by1080 == "cpu" and 65 <= n1080 <= 85
    assert 25 <= n4k <= 34 and n4k < n1080 / 2 + 5
    # 50개가 돌고 있는 지금(CPU 36.8%)
    now = capacity.compute(cfg, [5000] * 50, None, 260.0, 36.8, 3100.0, 7800.0, live_cpu=[2.9] * 50, running=50, cores=4)
    m1080, _ = capacity.add_by_profile(cfg, now, 5000, 128, 0, a_core, v_per)
    m4k, _ = capacity.add_by_profile(cfg, now, 14000, 200, 20, a_core, v_per)
    assert 15 <= m1080 <= 25 and 6 <= m4k <= 11
    # 나눠 잰 값이 없으면 라이브당 CPU 를 비트레이트 비율로 늘려 보수적으로
    g = capacity.add_by_profile(cfg, now, 14000, 200, 20)
    assert g is not None and g[0] <= m4k + 2
