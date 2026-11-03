import os
import time

import pytest

from livemax import cleanup


def _age(p, days):
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def test_safe_unlink_refuses_media_originals(paths):
    (paths.video / "morning").mkdir()
    f = paths.video / "morning" / "clip.mp4"
    f.write_bytes(b"x")
    with pytest.raises(cleanup.ProtectedPathError):
        cleanup.safe_unlink(paths, f, cleanup.Report(), "test")
    assert f.exists()


def test_rejected_and_thumbs_cleaned_but_originals_kept(paths):
    rej = paths.rejected / "video" / "morning"
    rej.mkdir(parents=True)
    old, new = rej / "old.mp4", rej / "new.mp4"
    old.write_bytes(b"x"); new.write_bytes(b"x")
    _age(old, 8)

    (paths.video / "morning").mkdir()
    orig = paths.video / "morning" / "keep.mp4"
    orig.write_bytes(b"x")
    _age(orig, 400)  # 아무리 오래돼도 원본은 지우지 않는다

    th = paths.thumbs_dir / "video" / "morning"
    th.mkdir(parents=True)
    (th / "keep.mp4.jpg").write_bytes(b"j")
    (th / "gone.mp4.jpg").write_bytes(b"j")  # 원본 없음 → 고아

    rep = cleanup.Report()
    now = time.time()
    cleanup.clean_rejected(paths, rep, False, now, 7)
    cleanup.clean_thumbs(paths, rep, False)
    assert not old.exists() and new.exists()
    assert (th / "keep.mp4.jpg").exists() and not (th / "gone.mp4.jpg").exists()
    assert orig.exists()


def test_concat_keeps_current_and_today(paths):
    d = paths.concat_dir / "ch01"
    d.mkdir(parents=True)
    today = time.strftime("%Y%m%d", time.gmtime())
    cur = d / f"{today}T041700Z.ffconcat"
    old = d / "20200101T041700Z.ffconcat"
    old_json = d / "20200101T041700Z.json"
    for f in (cur, old, old_json):
        f.write_text("x")
    (d / "current.ffconcat").symlink_to(cur.name)
    cleanup.clean_concat(paths, cleanup.Report(), False, time.time())
    assert cur.exists() and not old.exists() and not old_json.exists()
