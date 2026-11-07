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
