"""v2.3: 4K·60fps 허용, 작은 썸네일, 로그 크기 제한, 지난 목록 즉시 삭제, DB 정리, 알림 지우기, 서버명."""

import shutil
import subprocess
import time

import pytest

from conftest import make_live
from livemax import cleanup, concat, db, probe, thumbs

HAS_X264 = bool(shutil.which("ffmpeg")) and "libx264" in subprocess.run(
    ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout if shutil.which("ffmpeg") else False


def _clip(path, size, fps):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d=4",
                    "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p", "-bf", "0",
                    "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
                    "-b:v", "6000k", "-minrate", "6000k", "-maxrate", "6000k", "-bufsize", "6000k", str(path)], check=True)


@pytest.mark.skipif(not HAS_X264, reason="ffmpeg/libx264 없음")
@pytest.mark.parametrize("size,fps,ok", [("3840x2160", 30, True), ("1920x1080", 60, True), ("1000x700", 30, False)])
def test_video_spec_allows_4k_and_60fps(tmp_path, settings, size, fps, ok):
    p = tmp_path / "c.mp4"
    _clip(p, size, fps)
    r = probe.validate_video(p, settings["spec"]["video"])
    assert r.ok is ok, r.reasons
    if ok:
        t = tmp_path / "t.jpg"
        assert thumbs.make_thumbnail(p, t) and t.stat().st_size < thumbs.MAX_BYTES  # 4K 라도 작은 썸네일


def test_log_trim_keeps_tail(paths, settings):
    f = paths.logs / "audio-live1.log"
    f.write_bytes(b"old line\n" * 2_000_000 + b"LAST LINE\n")  # 약 18MB
    keep = paths.logs / "livemaxd.log"
    keep.write_text("small\n")
    rep = cleanup.Report()
    cleanup.clean_logs(paths, rep, False, time.time(), 14, max_mb=10, keep_mb=2, total_mb=300)
    assert f.stat().st_size <= 2_100_000 and f.read_text().endswith("LAST LINE\n")
    assert keep.read_text() == "small\n"
    with open(f, "a") as fh:  # 추가 모드 쓰기는 줄인 뒤에도 끝에 붙는다
        fh.write("after\n")
    assert f.read_text().endswith("LAST LINE\nafter\n")


def test_old_concat_lists_removed_immediately(paths):
    from datetime import datetime, timezone

    lv = make_live()
    e = [concat.Entry(paths.video / "a.mp4", 60, 0)]
    first = concat.write(lv, paths, e, datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = concat.write(lv, paths, e, datetime(2026, 1, 2, tzinfo=timezone.utc))
    names = sorted(p.name for p in (paths.concat_dir / "live1").iterdir())
    assert names == sorted(["current.ffconcat", second.name, second.with_suffix(".json").name]) and not first.exists()


def test_db_retention(paths, settings):
    now = time.time()
    with db.session(paths.db) as c:
        c.execute("INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)", (now - 40 * 86400, "info", "x", "old"))
        c.execute("INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)", (now, "info", "x", "new"))
        c.execute("INSERT INTO metrics_live(ts, live_id, state) VALUES (?,?,?)", (now - 5 * 86400, "live1", "running"))
        cleanup.rollup_metrics(c, 3, now, False)
        assert [r[0] for r in c.execute("SELECT message FROM events")] == ["new"]
        assert c.execute("SELECT COUNT(*) FROM metrics_live").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM metrics_live_hourly").fetchone()[0] == 1


def test_alert_dismiss_and_server_name(client):
    from test_api import ORIGIN, _ready

    _ready(client)
    with db.session(client.core.paths.db) as c:
        db.add_event(c, "error", "live", "ffmpeg 종료", "live1")
        db.add_event(c, "warn", "live", "속도 저하", "live1")
    alerts = [a for a in client.get("/api/overview").json()["alerts"] if a.get("id")]
    assert len(alerts) == 2
    assert client.delete(f"/api/alerts/{alerts[0]['id']}", headers=ORIGIN).json()["ok"]
    assert len([a for a in client.get("/api/overview").json()["alerts"] if a.get("id")]) == 1
    client.delete("/api/alerts", headers=ORIGIN)
    assert not [a for a in client.get("/api/overview").json()["alerts"] if a.get("id")]

    assert client.put("/api/system/settings", headers=ORIGIN, json={"server_name": "<b>"}).status_code == 400
    assert client.put("/api/system/settings", headers=ORIGIN, json={"server_name": "LIVE 1"}).json()["ok"]
    assert client.get("/api/session").json()["server_name"] == "LIVE 1"
    assert client.get("/api/system").json()["notify"]["label"] == "LIVE 1"
