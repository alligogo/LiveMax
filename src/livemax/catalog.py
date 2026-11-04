"""미디어 카탈로그 — 폴더 안 파일 목록과 ffprobe 결과(길이·비트레이트) 캐시.

concat 목록 생성 때마다 수천 개 클립을 ffprobe 하지 않도록 DB 에 캐시한다.
캐시가 없거나 파일이 바뀌었으면(크기·mtime) 그때만 다시 읽는다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import probe
from .paths import Paths

VIDEO_GLOB = ("*.mp4", "*.mkv")
AUDIO_GLOB = ("*.m4a", "*.aac")


def natural_key(name: str) -> list:
    """사람이 기대하는 순서: '비디오 2' < '비디오 10' (숫자는 크기로, 글자는 대소문자 무시)."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", name) if t != ""]


def list_folders(paths: Paths, kind: str) -> list[str]:
    root = paths.kind_root(kind)
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))), key=natural_key)


def list_files(paths: Paths, kind: str, folder: str) -> list[Path]:
    d = paths.kind_root(kind) / folder
    if not d.is_dir():
        return []
    globs = VIDEO_GLOB if kind == "video" else AUDIO_GLOB
    files: list[Path] = []
    for g in globs:
        files.extend(p for p in d.glob(g) if p.is_file() and not p.name.startswith("."))
    return sorted(files, key=lambda p: natural_key(p.name))


def upsert(conn: sqlite3.Connection, kind: str, folder: str, path: Path, duration: float | None,
           bitrate_kbps: float | None, info: dict | None = None, thumb: str | None = None) -> None:
    st = path.stat()
    conn.execute(
        """INSERT INTO media_files(path, kind, folder, name, size, mtime, duration, bitrate_kbps, info, thumb, added_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, duration=excluded.duration,
             bitrate_kbps=excluded.bitrate_kbps, info=excluded.info, thumb=COALESCE(excluded.thumb, media_files.thumb)""",
        (str(path), kind, folder, path.name, st.st_size, st.st_mtime, duration, bitrate_kbps,
         json.dumps(info or {}, ensure_ascii=False), thumb, time.time()),
    )


def remove(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("DELETE FROM media_files WHERE path=?", (str(path),))


def get_duration(conn: sqlite3.Connection, kind: str, folder: str, path: Path) -> float | None:
    """캐시된 길이. 없거나 파일이 바뀌었으면 ffprobe 로 다시 읽어 캐시한다."""
    st = path.stat()
    row = conn.execute("SELECT size, mtime, duration FROM media_files WHERE path=?", (str(path),)).fetchone()
    if row and row["duration"] and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1:
        return float(row["duration"])
    dur = probe.duration_of(path)
    if dur:
        upsert(conn, kind, folder, path, dur, None)
    return dur


def get_bitrate(conn: sqlite3.Connection, path: Path) -> float | None:
    row = conn.execute("SELECT bitrate_kbps FROM media_files WHERE path=?", (str(path),)).fetchone()
    return float(row["bitrate_kbps"]) if row and row["bitrate_kbps"] else None
