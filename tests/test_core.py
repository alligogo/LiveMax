"""v2 핵심: 설정 자동 배정, 영상 목록, 정리 안전장치, 감시 판정, 용량, 마스킹."""

import logging
import os
import time
from datetime import datetime, timezone

import pytest

from livemax import capacity, catalog, cleanup, concat, config, db, secrets
from livemax.config import ConfigError, LiveStore
from conftest import make_live


# ---------------------------------------------------------------- 설정
def test_store_auto_assigns_id_port_slot(paths, settings):
    st = LiveStore(paths, settings)
    a = st.new("A"); a.video_folders, a.audio_folders = ["v"], ["a"]; st.save(a)
    b = st.new("B"); b.video_folders, b.audio_folders = ["v"], ["a"]; st.save(b)
    assert (a.id, b.id) == ("live1", "live2")
    assert a.harbor_port != b.harbor_port and a.restart_slot != b.restart_slot
    st.delete("live1")
    c = st.new("C")
    assert c.id == "live1" and c.harbor_port == a.harbor_port  # 빈 번호 재사용


def test_restart_times_are_staggered(settings):
    times = {make_live(id=f"live{i}", restart_slot=i).restart_time_utc(settings) for i in range(15)}
    assert len(times) == 15  # 같은 시각 재시작 없음 (DESIGN 11-6)
    assert make_live(restart_slot=1).restart_time_utc(settings) == "15:02"  # 기본 15:00 UTC = 한국 자정


@pytest.mark.parametrize("field,value", [("video_folders", []), ("audio_folders", []), ("name", " "),
                                         ("audio_mode", "x"), ("crossfade", 11), ("timer_mode", "x"),
                                         ("timer_gap_minutes", 0)])
def test_validation_rejects(field, value):
    lv = make_live(**{field: value})
    with pytest.raises(ConfigError):
        lv.validate()


def test_timer_end_at():
    assert make_live().timer_end_at(1000.0) is None
    assert make_live(timer_mode="duration", timer_minutes=710).timer_end_at(1000.0) == 1000.0 + 710 * 60
    assert make_live(timer_mode="until", timer_until=5000.0).timer_end_at(1000.0) == 5000.0
    with pytest.raises(ConfigError):
        make_live(timer_mode="until", timer_until=0).validate()
    with pytest.raises(ConfigError):
        make_live(timer_mode="duration", timer_minutes=5).validate()


def test_old_yaml_with_bitrate_still_loads():
    lv = config.live_from_dict({"id": "live1", "name": "x", "bitrate": "5500k", "video_folders": ["a"],
                                "audio_folders": ["b"], "harbor_port": 8101})
    assert lv.timer_mode == "off" and "bitrate" not in lv.to_dict()


@pytest.mark.parametrize("name,ok", [("morning", True), ("저녁 재즈", True), ("_incoming", False), ("a/b", False), ("x#{y}", False)])
def test_folder_names(name, ok):
    assert config.valid_folder_name(name) is ok


# ---------------------------------------------------------------- 영상 목록
def _clips(paths, folders, n=3, dur=60.0):
    conn = db.connect(paths.db)
    for f in folders:
        (paths.video / f).mkdir(parents=True, exist_ok=True)
        for i in range(n):
            p = paths.video / f / f"{f}{i}.mp4"
            p.write_bytes(b"x")
            catalog.upsert(conn, "video", f, p, dur, 5500)
    return conn


def test_concat_covers_24h_without_back_to_back_repeats(paths, settings):
    conn = _clips(paths, ["morning", "night"], n=3, dur=59.9)
    lv = make_live(video_folders=["morning", "night"])
    entries = concat.build(lv, paths, conn, 24, 45, seed=1)
    assert entries[-1].offset + entries[-1].duration >= 24 * 3600 + 45 * 60
    assert all(a.path != b.path for a, b in zip(entries, entries[1:]))
    assert {e.path.parent.name for e in entries} == {"morning", "night"}


def test_concat_empty_folder_errors(paths, settings):
    conn = db.connect(paths.db)
    with pytest.raises(concat.ConcatError):
        concat.build(make_live(video_folders=["none"]), paths, conn)


def test_concat_current_clip(paths, settings):
    conn = _clips(paths, ["morning"], n=2)
    lv = make_live()
    entries = concat.build(lv, paths, conn, 1, 0)
    p = concat.write(lv, paths, entries, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert concat.current_clip(p.with_suffix(".json"), 125)["path"] == str(entries[2].path)


# ---------------------------------------------------------------- 정리 안전장치
def test_cleanup_never_deletes_media_originals(paths):
    (paths.video / "m").mkdir()
    f = paths.video / "m" / "clip.mp4"
    f.write_bytes(b"x")
    with pytest.raises(cleanup.ProtectedPathError):
        cleanup.safe_unlink(paths, f, cleanup.Report(), "t")
    assert f.exists()


def test_cleanup_run_removes_old_rejected_keeps_originals(paths, settings):
    (paths.rejected / "video").mkdir(parents=True)
    old = paths.rejected / "video" / "old.mp4"; old.write_bytes(b"x")
    t = time.time() - 8 * 86400; os.utime(old, (t, t))
    (paths.video / "m").mkdir(); orig = paths.video / "m" / "keep.mp4"; orig.write_bytes(b"x"); os.utime(orig, (t, t))
    cleanup.run(paths, settings)
    assert not old.exists() and orig.exists()


# ---------------------------------------------------------------- 감시 판정
class _FakeSup:
    def __init__(self, paths, settings):
        self.paths, self.settings = paths, settings


def _proc(paths, settings, **st):
    from livemax.supervisor import LiveProc
    p = LiveProc(_FakeSup(paths, settings), "live1")
    p.desired = True
    now = 1_000_000.0
    p.st.update({"state": "running", "ffmpeg_started_at": now - 600, "progress_at": now - 1, "speed": 1.0, **st})
    return p, now


def test_watch_healthy(paths, settings):
    p, now = _proc(paths, settings)
    assert p.watch(now) is None


def test_watch_stall(paths, settings):
    p, now = _proc(paths, settings, progress_at=1_000_000.0 - 40)
    assert "정지" in p.watch(now)


def test_watch_low_speed_needs_grace(paths, settings):
    p, now = _proc(paths, settings, speed=0.8)
    assert p.watch(now) is None
    p.st["progress_at"] = now + 59
    assert "speed" in p.watch(now + 61)


def test_watch_startup_grace(paths, settings):
    p, now = _proc(paths, settings, ffmpeg_started_at=1_000_000.0 - 5, progress_at=1_000_000.0 - 100)
    assert p.watch(now) is None


# ---------------------------------------------------------------- 용량 / 비밀
def test_capacity_traffic_bound(settings):
    cap = capacity.compute(settings["capacity"], [5500] * 10, None, 58, 10, 3000, 8000,
                           now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert 17 < cap.projected_tb < 20 and cap.limiting == "traffic"
    # 목표 28TB 까지: 10개(약 18.9TB) + 라이브당 약 1.9TB → 4개 더
    assert cap.target_tb == 28 and cap.limit_tb == 32
    assert cap.can_add == int((28 - cap.projected_tb) // cap.per_live_tb)


def test_mask_and_redact(caplog):
    key = "abcd-efgh-ijkl-1a2b"
    assert secrets.mask(key) == "****1a2b"
    secrets.register_secret(key)
    lg = logging.getLogger("t"); lg.addFilter(secrets.Redactor())
    with caplog.at_level(logging.INFO):
        lg.info("rtmps://a.rtmps.youtube.com/live2/%s", key)
    assert key not in caplog.text


def test_m3u_mixes_all_songs_of_all_folders(paths):
    """여러 폴더를 골라도 곡 단위로 섞이도록 한 목록 (폴더별 50% 비중 버그 방지)."""
    from livemax import liquidsoap

    for folder, n in (("big", 5), ("small", 2)):
        (paths.audio / folder).mkdir(parents=True)
        for i in range(n):
            (paths.audio / folder / f"{folder}{i}.m4a").write_bytes(b"x")
    lv = make_live(audio_folders=["big", "small"])
    assert liquidsoap.write_m3u(lv, paths) is True
    m3u = liquidsoap.m3u_path(lv, paths)
    lines = m3u.read_text().splitlines()
    assert len(lines) == 7 and all(line.startswith("/") for line in lines)
    ino = m3u.stat().st_ino
    assert liquidsoap.write_m3u(lv, paths) is False  # 안 바뀌면 건드리지 않음
    (paths.audio / "small" / "small1.m4a").unlink()
    assert liquidsoap.write_m3u(lv, paths) is True
    assert m3u.stat().st_ino == ino and len(m3u.read_text().splitlines()) == 6  # 같은 파일에 덮어씀 (감시 유지)
    liq = liquidsoap.render(lv, paths, settings={"spec": {"audio": {"bitrate": "128k", "sample_rate": 44100, "channels": 2}}})
    assert liq.count("playlist(") == 1 and "random([" not in liq and "track_sensitive=false" in liq
