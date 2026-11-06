"""다음 곡·다음 영상 미리 읽기."""

import json
import threading
from pathlib import Path

from livemax import concat, prefetch


def _meta(tmp_path: Path, clips: list[tuple[str, float]]) -> Path:
    t, ents = 0.0, []
    for name, d in clips:
        p = tmp_path / name
        p.write_bytes(b"x" * 1000)
        ents.append({"path": str(p), "duration": d, "offset": t})
        t += d
    m = tmp_path / "list.json"
    m.write_text(json.dumps({"entries": ents}), encoding="utf-8")
    concat._META_CACHE.clear()
    return m


def test_upcoming_clips_covers_next_90_seconds(tmp_path):
    m = _meta(tmp_path, [("a.mp4", 60), ("b.mp4", 10), ("c.mp4", 10), ("d.mp4", 200), ("e.mp4", 60)])
    got = [Path(p).name for p in prefetch.upcoming_clips(m, 30)]
    assert got == ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]  # 지금(a) + 30~120초 안에 시작하는 것들
    assert [Path(p).name for p in prefetch.upcoming_clips(m, 250)] == ["d.mp4", "e.mp4"]
    assert prefetch.upcoming_clips(None, 10) == []


def test_upcoming_clips_caps_short_clip_lists(tmp_path):
    m = _meta(tmp_path, [(f"{i}.mp4", 3) for i in range(40)])
    assert len(prefetch.upcoming_clips(m, 0)) == prefetch.MAX_FILES


def test_warm_handles_missing_and_real_files(tmp_path):
    f = tmp_path / "s.m4a"
    f.write_bytes(b"y" * 4096)
    assert prefetch.warm(f) is True
    assert prefetch.warm(tmp_path / "없음.m4a") is False


def test_files_for_reads_now_and_next_song_and_clips(tmp_path):
    m = _meta(tmp_path, [("a.mp4", 60), ("b.mp4", 60)])
    run = tmp_path / "run"
    run.mkdir()
    (run / "live1.nowplaying.json").write_text(json.dumps({"file": "/m/now.m4a"}))
    (run / "live1.next.json").write_text(json.dumps({"file": "/m/next.m4a"}))

    class Sup:
        class paths:
            run_dir = run

    class Proc:
        id, sup, lock, meta_path, st = "live1", Sup, threading.Lock(), m, {"out_time": 50.0}

    got = prefetch.files_for(Proc)
    assert got[:2] == ["/m/now.m4a", "/m/next.m4a"]
    assert [Path(p).name for p in got[2:]] == ["a.mp4", "b.mp4"]
