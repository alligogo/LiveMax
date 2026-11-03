"""영상 편성 — 24시간치 concat 목록 사전 생성 (DESIGN 4.4, 4.5).

시간대 전환마다 재시작하면 끊기므로, 시작 시점부터 24시간(+여유) 동안의 클립 순서를 미리 만든다.
ffmpeg 는 목록을 순서대로 재생할 뿐이지만, 결과적으로 정해진 시각에 영상이 바뀐다.

- 각 클립의 "방송 시각"은 누적 길이로 계산하고, 그 시각을 타깃 시간대(지역명)로 변환해
  해당 구간의 폴더에서 클립을 고른다. 서머타임은 zoneinfo 가 처리한다 (오프셋 하드코딩 금지).
- 구간 안에서는 셔플백(한 바퀴 돌기 전엔 중복 없음)으로 섞는다.
- runner 가 시작할 때마다 "지금"부터 새로 만든다. 그래서 장애 재시작 후에도 시각이 맞는다.
- 목록 옆에 .json 메타(각 항목의 시작 오프셋)를 두어 "지금 나가는 클립"을 계산한다 (캡처 없이).
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import catalog
from .config import LiveConfig
from .paths import Paths
from .state import write_json_atomic

log = logging.getLogger("livemax.concat")


class ConcatError(Exception):
    pass


@dataclass
class Entry:
    path: Path
    duration: float
    offset: float  # 목록 시작부터의 초
    segment: str


def _escape(p: Path) -> str:
    # ffconcat 은 작은따옴표 안에서 ' 를 '\'' 로 이스케이프
    return str(p).replace("'", "'\\''")


class _Bag:
    """폴더 묶음에서 중복 없이 섞어 꺼내는 셔플백."""

    def __init__(self, files: list[Path], rng: random.Random):
        self.files = files
        self.rng = rng
        self.queue: list[Path] = []
        self.last: Path | None = None

    def next(self) -> Path:
        if not self.queue:
            self.queue = self.files[:]
            self.rng.shuffle(self.queue)
            # 바퀴 경계에서 같은 클립이 연달아 나오지 않게
            if len(self.queue) > 1 and self.queue[-1] == self.last:
                self.queue[0], self.queue[-1] = self.queue[-1], self.queue[0]
        p = self.queue.pop()
        self.last = p
        return p


def build(live: LiveConfig, paths: Paths, conn: sqlite3.Connection, start_utc: datetime,
          hours: float = 24, margin_minutes: float = 45, seed: int | None = None) -> list[Entry]:
    rng = random.Random(seed)
    total = hours * 3600 + margin_minutes * 60
    bags: dict[str, _Bag] = {}
    durations: dict[Path, float] = {}
    entries: list[Entry] = []
    excluded: set[Path] = set(
        Path(r["path"]) for r in conn.execute("SELECT path FROM pending_deletes").fetchall()
    )

    def bag_for(seg_key: str, folders: list[str]) -> _Bag:
        if seg_key not in bags:
            files: list[Path] = []
            for f in folders:
                files.extend(p for p in catalog.list_files(paths, "video", f) if p not in excluded)
            if not files:
                raise ConcatError(f"{live.id}: 구간 {seg_key} 의 영상 폴더 {folders} 에 클립이 없음")
            bags[seg_key] = _Bag(files, rng)
        return bags[seg_key]

    t = 0.0
    low_bitrate_warned: set[Path] = set()
    while t < total:
        when = start_utc + timedelta(seconds=t)
        seg = live.segment_at(when)
        key = seg.start
        clip = bag_for(key, seg.video).next()
        folder = clip.parent.name
        if clip not in durations:
            d = catalog.get_duration(conn, "video", folder, clip)
            if not d or d <= 0.5:
                log.warning("길이를 알 수 없는 클립 제외: %s", clip)
                bags[key].files = [f for f in bags[key].files if f != clip]
                if not bags[key].files:
                    raise ConcatError(f"{live.id}: 구간 {key} 에 사용 가능한 클립이 없음")
                continue
            durations[clip] = d
            br = catalog.get_bitrate(conn, clip)
            # 비트레이트는 라이브별 설정값 (DESIGN 5.1). copy 송출이므로 클립 자체의 비트레이트가 곧 송출 비트레이트.
            if br and br < live.bitrate_kbps * 0.9 and clip not in low_bitrate_warned:
                low_bitrate_warned.add(clip)
                log.warning("%s: 클립 비트레이트 %.0fk < 라이브 설정 %dk — %s", live.id, br, live.bitrate_kbps, clip.name)
        entries.append(Entry(clip, durations[clip], t, key))
        t += durations[clip]
    return entries


def write(live: LiveConfig, paths: Paths, entries: list[Entry], start_utc: datetime) -> Path:
    out_dir = paths.concat_dir / live.id
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = start_utc.strftime("%Y%m%dT%H%M%SZ")
    list_path = out_dir / f"{stamp}.ffconcat"
    tmp = list_path.with_suffix(".tmp")
    lines = ["ffconcat version 1.0"]
    for e in entries:
        # duration 지시어는 넣지 않는다: 실제 패킷 타임스탬프를 그대로 쓰는 편이 이음매가 정확하다.
        lines.append(f"file '{_escape(e.path)}'")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(list_path)

    meta = {
        "live_id": live.id,
        "start_utc": start_utc.isoformat(),
        "timezone": live.timezone,
        "entries": [{"path": str(e.path), "offset": round(e.offset, 3), "duration": round(e.duration, 3), "segment": e.segment}
                    for e in entries],
    }
    write_json_atomic(list_path.with_suffix(".json"), meta)
    # 현재 목록을 가리키는 심볼릭 링크 (cleanup 이 이 목록은 지우지 않는다)
    cur = out_dir / "current.ffconcat"
    tmp_link = out_dir / ".current.tmp"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(list_path.name)
    tmp_link.replace(cur)
    return list_path


def generate(live: LiveConfig, paths: Paths, conn: sqlite3.Connection, server: dict,
             start_utc: datetime | None = None) -> Path:
    start_utc = start_utc or datetime.now(timezone.utc).replace(microsecond=0)
    hours = float(server["concat"]["hours"])
    margin = float(server["concat"]["margin_minutes"])
    entries = build(live, paths, conn, start_utc, hours, margin)
    p = write(live, paths, entries, start_utc)
    by_seg: dict[str, int] = {}
    for e in entries:
        by_seg[e.segment] = by_seg.get(e.segment, 0) + 1
    log.info("%s: concat 목록 생성 %s (%d개 클립, %.1f시간, 구간별 %s)", live.id, p.name, len(entries),
             (entries[-1].offset + entries[-1].duration) / 3600 if entries else 0, by_seg)
    return p


def current_clip(meta_path: Path, out_time_s: float) -> dict | None:
    """ffmpeg 의 out_time 으로 지금 나가는 클립을 계산 (서버 캡처 없이 — DESIGN 8.4)."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    lo, hi = 0, len(meta["entries"]) - 1
    ents = meta["entries"]
    if hi < 0:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ents[mid]["offset"] <= out_time_s:
            lo = mid
        else:
            hi = mid - 1
    return ents[lo]
