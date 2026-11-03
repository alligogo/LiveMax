from datetime import datetime, timedelta, timezone

from livemax import catalog, concat, config, db
from conftest import live_dict


def _setup(paths, folders: dict[str, int], dur=60.0):
    conn = db.connect(paths.db)
    for folder, n in folders.items():
        d = paths.video / folder
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            f = d / f"{folder}_{i:02d}.mp4"
            f.write_bytes(b"x")
            catalog.upsert(conn, "video", folder, f, dur, 5500)
    return conn


def test_schedule_switches_at_local_time(paths, server):
    conn = _setup(paths, {"morning": 5, "evening": 5})
    lv = config.parse_live(live_dict(), None, server)
    # 시카고 겨울(CST). 시작 = 현지 17:30 → 30분 뒤 18:00 에 evening 으로 바뀌어야 함
    start = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    entries = concat.build(lv, paths, conn, start, hours=1, margin_minutes=0, seed=1)
    assert len(entries) == 60
    for e in entries:
        local = (start + timedelta(seconds=e.offset)).astimezone(lv.tz)
        want = "morning" if local.hour < 18 and local.hour >= 6 else "evening"
        assert e.path.parent.name == want, (local, e.path)


def test_list_covers_24h_plus_margin(paths, server):
    conn = _setup(paths, {"morning": 3, "evening": 3}, dur=59.9)
    lv = config.parse_live(live_dict(), None, server)
    start = datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)  # 서머타임 시작일
    entries = concat.build(lv, paths, conn, start, hours=24, margin_minutes=45, seed=2)
    total = entries[-1].offset + entries[-1].duration
    assert total >= 24 * 3600 + 45 * 60
    # 같은 클립이 연달아 나오지 않음 (셔플백)
    assert all(a.path != b.path for a, b in zip(entries, entries[1:]))


def test_pending_delete_excluded(paths, server):
    conn = _setup(paths, {"morning": 2, "evening": 2})
    victim = paths.video / "morning" / "morning_00.mp4"
    conn.execute("INSERT INTO pending_deletes(path, requested_at) VALUES (?, 0)", (str(victim),))
    lv = config.parse_live(live_dict(), None, server)
    entries = concat.build(lv, paths, conn, datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc), hours=2, margin_minutes=0)
    assert victim not in {e.path for e in entries}


def test_write_and_current_clip(paths, server):
    conn = _setup(paths, {"morning": 2, "evening": 2})
    lv = config.parse_live(live_dict(), None, server)
    start = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    entries = concat.build(lv, paths, conn, start, hours=1, margin_minutes=0)
    p = concat.write(lv, paths, entries, start)
    text = p.read_text()
    assert text.startswith("ffconcat version 1.0")
    assert (p.parent / "current.ffconcat").resolve() == p.resolve()
    c = concat.current_clip(p.with_suffix(".json"), 125.0)
    assert c["path"] == str(entries[2].path)
