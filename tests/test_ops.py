import logging
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from livemax import capacity, config, liquidsoap, secrets
from livemax.watchdog import LiveWatch, evaluate
from conftest import live_dict

WD = config.DEFAULT_SERVER["watchdog"]


def _st(**kw):
    now = 1_000_000.0
    d = {"state": "running", "ffmpeg_started_at": now - 600, "updated_at": now - 1, "speed": 1.0}
    d.update(kw)
    return d, now


def test_watchdog_healthy():
    st, now = _st()
    assert evaluate(st, LiveWatch(), WD, now) is None


def test_watchdog_stall():
    st, now = _st(updated_at=1_000_000.0 - 120)
    assert "정지" in evaluate(st, LiveWatch(), WD, now)


def test_watchdog_low_speed_needs_grace():
    w = LiveWatch()
    st, now = _st(speed=0.8)
    assert evaluate(st, w, WD, now) is None  # 처음엔 기록만
    st["updated_at"] = now + 59
    assert evaluate(st, w, WD, now + 60) is not None


def test_watchdog_startup_grace():
    st, now = _st(ffmpeg_started_at=1_000_000.0 - 10, updated_at=1_000_000.0 - 100)
    assert evaluate(st, LiveWatch(), WD, now) is None


def test_capacity_traffic_is_bottleneck():
    cfg = config.DEFAULT_SERVER["capacity"]
    cap = capacity.compute(cfg, [5500] * 10, None, 58.0, 10.0, 4500, 8000,
                           now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert 17 < cap.projected_tb < 20  # DESIGN 2.3: 라이브 10개 ≈ 17.8TB/월
    assert cap.limiting == "traffic"
    assert 0 <= cap.can_add <= 8


def test_mask_and_redact(caplog):
    key = "abcd-efgh-ijkl-1a2b"
    assert secrets.mask(key) == "****1a2b"
    secrets.register_secret(key)
    lg = logging.getLogger("t")
    lg.addFilter(secrets.Redactor())
    with caplog.at_level(logging.INFO):
        lg.info("out rtmps://a.rtmp.youtube.com/live2/%s", key)
    assert key not in caplog.text and "****1a2b" in caplog.text


@pytest.mark.skipif(not shutil.which("liquidsoap"), reason="liquidsoap 미설치")
@pytest.mark.parametrize("crossfade", [0, 3])
def test_rendered_liq_typechecks(paths, server, tmp_path, crossfade):
    """생성된 스크립트가 설치된 Liquidsoap 버전에서 실제로 타입체크를 통과하는지 (copy / 크로스페이드 양쪽)."""
    lv = config.parse_live(live_dict(audio={"mode": "random_norepeat", "crossfade": crossfade, "harbor_port": 8101}, schedule=[
        {"from": "06:00", "video": ["m"], "audio": ["upbeat", "focus"]},
        {"from": "23:00", "video": ["n"], "audio": ["ambient"]},
    ]), None, server)
    liq = tmp_path / "t.liq"
    liq.write_text("settings.init.allow_root.set(true)\n" + liquidsoap.render(lv, paths, server))
    r = subprocess.run(["liquidsoap", "--check", str(liq)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
