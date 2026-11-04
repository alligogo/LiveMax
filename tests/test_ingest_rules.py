"""같은 이름은 무조건 거부(덮어쓰기·번호 붙이기 없음), 방송 폴더 직접 변경 감시. (내용 중복 검사는 CPU 절약으로 뺌)"""

import shutil
import subprocess
import time

import pytest

from livemax import db, ingest

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def _setup(paths, name="재즈"):
    (paths.audio / name).mkdir(parents=True, exist_ok=True)
    (paths.incoming / "audio" / name).mkdir(parents=True, exist_ok=True)
    return paths.audio / name, paths.incoming / "audio" / name


def _rej(conn):
    return [r["reasons"] for r in conn.execute("SELECT reasons FROM rejections ORDER BY id")]


@pytest.mark.parametrize("existing,incoming,content_same,expect", [
    ("Song.m4a", "Song.m4a", True, "같은 이름의 파일"),
    ("Song.m4a", "song.M4A", False, "같은 이름의 파일"),   # 대소문자만 달라도 같은 이름
])
def test_name_collision_rejected(paths, settings, existing, incoming, content_same, expect):
    live, inc = _setup(paths)
    (live / existing).write_bytes(b"A" * 1000)
    (inc / incoming).write_bytes(b"A" * 1000 if content_same else b"B" * 1000)
    with db.session(paths.db) as conn:
        ok, reasons = ingest.process_file(paths, settings, conn, "audio", "재즈", inc / incoming)
        assert not ok and expect in reasons[0]
        assert expect in _rej(conn)[-1]
    assert (live / existing).read_bytes() == b"A" * 1000          # 기존 파일은 그대로
    assert sorted(p.name for p in live.iterdir()) == [existing]  # 번호 붙은 사본 없음
    assert not (inc / incoming).exists()                           # 대기 폴더에서 격리로 이동


def test_web_upload_refuses_existing_name(client):
    from test_api import ORIGIN, _ready

    _ready(client)
    (client.core.paths.audio / "재즈-색소폰" / "Hollow Stars.m4a").write_bytes(b"x")
    r = client.post("/api/uploads", headers=ORIGIN, json={"kind": "audio", "folder": "재즈-색소폰", "name": "hollow stars.m4a", "size": 10})
    assert r.status_code == 400 and "같은 이름" in r.json()["error"] and "방송 폴더" in r.json()["error"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_watch_broadcast_registers_and_flags(paths, settings):
    live, _ = _setup(paths)
    song = live / "direct.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "aac",
                    "-b:a", "128k", "-ar", "44100", "-ac", "2", str(song)], check=True)
    old = time.time() - 120
    import os
    os.utime(song, (old, old))
    memo: dict = {}
    with db.session(paths.db) as conn:
        assert ingest.watch_broadcast(paths, settings, conn, memo) == 1
        ev = [r["message"] for r in conn.execute("SELECT message FROM events WHERE source='media-watch'")]
        assert len(ev) == 1 and "direct.m4a" in ev[0] and "등록함" in ev[0]
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE path=?", (str(song),)).fetchone()[0] == 1
        assert ingest.watch_broadcast(paths, settings, conn, memo) == 0   # 같은 상태는 한 번만
        # 방송 폴더에서 직접 덮어쓰기 → 경고
        with open(song, "ab") as f:
            f.write(b"\0" * 10)
        os.utime(song, (old + 5, old + 5))
        assert ingest.watch_broadcast(paths, settings, conn, memo) == 1
        ev = [r["message"] for r in conn.execute("SELECT message FROM events WHERE source='media-watch' ORDER BY id")]
        assert "직접 바뀌었습니다" in ev[-1]
    assert song.exists()  # 원본은 절대 옮기거나 지우지 않음


def test_natural_sort(paths):
    from livemax import catalog

    for n in ("비디오 10", "비디오 2", "비디오 1", "비디오 9"):
        (paths.video / n).mkdir(parents=True)
    assert catalog.list_folders(paths, "video") == ["비디오 1", "비디오 2", "비디오 9", "비디오 10"]
    for n in ("곡 10.m4a", "곡 2.m4a", "곡 1.m4a"):
        (paths.audio / "a").mkdir(parents=True, exist_ok=True)
        (paths.audio / "a" / n).write_bytes(b"x")
    assert [p.name for p in catalog.list_files(paths, "audio", "a")] == ["곡 1.m4a", "곡 2.m4a", "곡 10.m4a"]
