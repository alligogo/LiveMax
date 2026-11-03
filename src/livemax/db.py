"""SQLite (지표 시계열 + 운영 기록 + 대시보드 보안 데이터).

여러 프로세스(collector, watchdog, ingest, dashboard)가 동시에 쓰므로 WAL + busy_timeout.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 7.1 시스템 지표 (원본, 30일 보관 후 시간 단위로 축약)
CREATE TABLE IF NOT EXISTS metrics_system (
    ts REAL NOT NULL,
    cpu_pct REAL, steal_pct REAL, iowait_pct REAL,
    mem_used_mb REAL, mem_total_mb REAL,
    disk_used_gb REAL, disk_total_gb REAL,
    net_tx_mbps REAL, net_rx_mbps REAL,
    month_tx_gb REAL,
    lives_running INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ms_ts ON metrics_system(ts);

-- 7.2 라이브별 지표
CREATE TABLE IF NOT EXISTS metrics_live (
    ts REAL NOT NULL,
    live_id TEXT NOT NULL,
    state TEXT, speed REAL,
    cpu_pct REAL, rss_mb REAL,
    audio_cpu_pct REAL, audio_rss_mb REAL,
    out_time_s REAL
);
CREATE INDEX IF NOT EXISTS ix_ml_ts ON metrics_live(ts);
CREATE INDEX IF NOT EXISTS ix_ml_live_ts ON metrics_live(live_id, ts);

-- 축약본 (시간 단위)
CREATE TABLE IF NOT EXISTS metrics_system_hourly (
    hour_ts INTEGER PRIMARY KEY,
    cpu_avg REAL, cpu_max REAL, steal_avg REAL, steal_max REAL,
    mem_used_avg REAL, disk_used_max REAL,
    net_tx_avg REAL, net_tx_max REAL, month_tx_gb_max REAL,
    lives_running_max INTEGER, samples INTEGER
);
CREATE TABLE IF NOT EXISTS metrics_live_hourly (
    hour_ts INTEGER NOT NULL, live_id TEXT NOT NULL,
    speed_avg REAL, speed_min REAL, cpu_avg REAL, rss_max REAL, samples INTEGER,
    PRIMARY KEY (hour_ts, live_id)
);

-- 운영 이벤트 (경고 목록의 원천)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
    live_id TEXT, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS restarts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, live_id TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_restarts_live_ts ON restarts(live_id, ts);

-- 미디어 카탈로그 (ffprobe 결과 캐시 — concat 목록 생성 시 재탐색을 피함)
CREATE TABLE IF NOT EXISTS media_files (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL, folder TEXT NOT NULL, name TEXT NOT NULL,
    size INTEGER, mtime REAL, duration REAL, bitrate_kbps REAL,
    info TEXT, thumb TEXT, added_at REAL
);
CREATE INDEX IF NOT EXISTS ix_media_kind_folder ON media_files(kind, folder);

CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, kind TEXT, folder TEXT, name TEXT,
    reasons TEXT NOT NULL, rejected_path TEXT
);

-- 사용자가 삭제를 요청했지만 오늘의 concat 목록이 아직 참조 중인 영상
CREATE TABLE IF NOT EXISTS pending_deletes (
    path TEXT PRIMARY KEY, requested_at REAL NOT NULL
);

-- 대시보드 보안 (DESIGN 9)
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, ip TEXT NOT NULL, username TEXT, success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_ip_ts ON login_attempts(ip, ts);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created REAL NOT NULL, last_seen REAL NOT NULL, expires REAL NOT NULL,
    ip TEXT, user_agent TEXT
);

CREATE TABLE IF NOT EXISTS allow_ips (
    ip TEXT PRIMARY KEY, note TEXT, created REAL NOT NULL, expires REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def add_event(conn: sqlite3.Connection, level: str, source: str, message: str, live_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO events(ts, level, source, live_id, message) VALUES (?,?,?,?,?)",
        (time.time(), level, source, live_id, message),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
