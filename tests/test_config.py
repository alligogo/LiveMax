from datetime import datetime, timezone

import pytest
import yaml

from livemax import config
from conftest import ROOT, live_dict


def test_example_config_parses(server):
    d = yaml.safe_load((ROOT / "config.example" / "ch01.yaml").read_text(encoding="utf-8"))
    lv = config.parse_live(d, None, server)
    assert lv.bitrate_kbps == 5500
    assert lv.audio_mode == "random_norepeat"
    assert lv.timezone == "America/Chicago"


@pytest.mark.parametrize("tz", ["UTC-6", "UTC+9", "GMT-5", "+09:00", "-6", "Etc/GMT+6"])
def test_offset_timezones_rejected(server, tz):
    with pytest.raises(config.ConfigError):
        config.parse_live(live_dict(timezone=tz), None, server)


def test_bitrate_is_per_live(server):
    a = config.parse_live(live_dict(video={"bitrate": "4500k"}), None, server)
    b = config.parse_live(live_dict(video={"bitrate": "5.5M"}), None, server)
    assert (a.bitrate_kbps, b.bitrate_kbps) == (4500, 5500)
    with pytest.raises(config.ConfigError):
        config.parse_live(live_dict(video={"bitrate": "500k"}), None, server)
    with pytest.raises(config.ConfigError):
        config.parse_live(live_dict(video={"bitrate": "fast"}), None, server)


def test_yaml_sexagesimal_trap_is_fixed(server):
    # 따옴표 없는 12:30 → PyYAML 은 750 (60진수) 으로 읽는다
    d = yaml.safe_load("restart_at: 12:30\nfrom: 18:00\n")
    assert d["restart_at"] == 750
    lv = config.parse_live(live_dict(restart_at=d["restart_at"],
                                     schedule=[{"from": d["from"], "video": ["v"], "audio": ["a"]}]), None, server)
    assert lv.restart_at == "12:30"
    assert lv.schedule[0].start == "18:00"


def _two(server, a_tz, a_at, b_tz, b_at):
    a = config.parse_live(live_dict(id="ch01", timezone=a_tz, restart_at=a_at), None, server)
    b = config.parse_live(live_dict(id="ch02", timezone=b_tz, restart_at=b_at,
                                    audio={"mode": "random", "harbor_port": 8102}), None, server)
    return config.cross_check([a, b], server)


def test_same_restart_time_rejected(server):
    assert any("겹침" in m for m in _two(server, "America/Chicago", "04:17", "America/Chicago", "04:17"))


def test_different_minutes_ok(server):
    assert _two(server, "America/Chicago", "04:17", "America/Chicago", "04:18") == []


def test_cross_timezone_collision_detected(server):
    # 시카고 04:17 == 뉴욕 05:17 (항상 같은 순간)
    assert _two(server, "America/Chicago", "04:17", "America/New_York", "05:17")


def test_dst_only_collision_detected(server):
    # 서울(서머타임 없음) vs 시카고(서머타임 있음): 1년 중 일부 기간에만 겹친다 → 그래도 잡아야 함
    # CDT(UTC-5): 시카고 04:00 = 09:00Z = 서울 18:00 / CST(UTC-6) 에서는 10:00Z 로 1시간 어긋남
    assert _two(server, "America/Chicago", "04:00", "Asia/Seoul", "18:00")


def test_duplicate_port_rejected(server):
    a = config.parse_live(live_dict(id="ch01", restart_at="01:00"), None, server)
    b = config.parse_live(live_dict(id="ch02", restart_at="02:00"), None, server)
    assert any("harbor_port" in m for m in config.cross_check([a, b], server))


@pytest.mark.parametrize("name,ok", [
    ("morning", True), ("저녁 재즈", True), ("lofi-2 (old)", True),
    ("_incoming", False), (".hidden", False), ("a/b", False), ("..", False), ("x#{y}", False), ('q"', False),
])
def test_folder_names(name, ok):
    assert config.valid_folder_name(name) is ok


def test_segment_lookup_wraps_midnight(server):
    lv = config.parse_live(live_dict(), None, server)
    # 시카고 03:00 (CST) = 09:00Z → 06:00 이전이므로 전날 18:00 구간
    assert lv.segment_at(datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)).start == "18:00"
    assert lv.segment_at(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)).start == "06:00"


def test_dst_shifts_utc_boundary(server):
    lv = config.parse_live(live_dict(), None, server)
    # 겨울(CST, UTC-6): 06:00 현지 = 12:00Z / 여름(CDT, UTC-5): 06:00 현지 = 11:00Z
    assert lv.segment_at(datetime(2026, 1, 15, 11, 30, tzinfo=timezone.utc)).start == "18:00"
    assert lv.segment_at(datetime(2026, 7, 15, 11, 30, tzinfo=timezone.utc)).start == "06:00"
