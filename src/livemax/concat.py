"""영상 목록 — 24시간치 concat 목록 사전 생성 (DESIGN 4.4, v2 단순화).

선택한 영상 폴더들의 클립을 셔플백(한 바퀴 돌기 전엔 중복 없음)으로 섞어 24시간+여유만큼 채운다.
ffmpeg 는 목록을 순서대로 copy 재생할 뿐이다. 송출 시작(재시작)할 때마다 새로 만든다.
목록 옆 .json 메타(각 항목 시작 오프셋)로 "지금 나가는 클립"을 계산한다 (서버 캡처 없이).
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import catalog
from .config import Live
from .paths import Paths
from .state import write_json_atomic

log = logging.getLogger("livemax.concat")


class ConcatError(Exception):
    pass


@dataclass
class Entry:
    path: Path
    duration: float
    offset: float


def _escape(p: Path) -> str:
    return str(p).replace("'", "'\\''")


def build(live: Live, paths: Paths, conn: sqlite3.Connection, hours: float = 24, margin_minutes: float = 45,
          seed: int | None = None) -> list[Entry]:
    rng = random.Random(seed)
    files: list[Path] = []
    for f in live.video_folders:
        files.extend(catalog.list_files(paths, "video", f))
    durations: dict[Path, float] = {}
    for p in files:
        d = catalog.get_duration(conn, "video", p.parent.name, p)
        if d and d > 0.5:
            durations[p] = d
        else:
            log.warning("%s: 길이를 알 수 없는 클립 제외: %s", live.id, p)
    usable = [p for p in files if p in durations]
    if not usable:
        raise ConcatError(f"선택한 비디오 폴더 {live.video_folders} 에 송출할 파일이 없습니다")

    total = hours * 3600 + margin_minutes * 60
    entries: list[Entry] = []
    bag: list[Path] = []
    last: Path | None = None
    t = 0.0
    while t < total:
        if not bag:
            bag = usable[:]
            rng.shuffle(bag)
            if len(bag) > 1 and bag[-1] == last:  # 바퀴 경계에서 같은 클립 연속 방지
                bag[0], bag[-1] = bag[-1], bag[0]
        clip = bag.pop()
        last = clip
        entries.append(Entry(clip, durations[clip], t))
        t += durations[clip]
    return entries


def write(live: Live, paths: Paths, entries: list[Entry], start_utc: datetime) -> Path:
    out_dir = paths.concat_dir / live.id
    out_dir.mkdir(parents=True, exist_ok=True)
    list_path = out_dir / f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}.ffconcat"
    tmp = list_path.with_suffix(".tmp")
    lines = ["ffconcat version 1.0"] + [f"file '{_escape(e.path)}'" for e in entries]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(list_path)
    write_json_atomic(list_path.with_suffix(".json"), {
        "live_id": live.id, "start_utc": start_utc.isoformat(),
        "entries": [{"path": str(e.path), "offset": round(e.offset, 3), "duration": round(e.duration, 3)} for e in entries],
    })
    cur = out_dir / "current.ffconcat"
    tmp_link = out_dir / ".current.tmp"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(list_path.name)
    tmp_link.replace(cur)
    # 지난 목록은 바로 지운다 (재시작마다 약 0.5MB씩 쌓이던 찌꺼기). 이전 ffmpeg 는 이미 끝난 뒤에 호출된다.
    keep = {list_path.name, list_path.with_suffix(".json").name, cur.name}
    for f in out_dir.iterdir():
        if f.name not in keep and f.suffix in (".ffconcat", ".json", ".tmp"):
            f.unlink(missing_ok=True)
    return list_path


def generate(live: Live, paths: Paths, conn: sqlite3.Connection, settings: dict) -> Path:
    start = datetime.now(timezone.utc).replace(microsecond=0)
    entries = build(live, paths, conn, float(settings["concat"]["hours"]), float(settings["concat"]["margin_minutes"]))
    p = write(live, paths, entries, start)
    log.info("%s: 영상 목록 생성 %s (%d개 클립, %.1f시간)", live.id, p.name, len(entries),
             (entries[-1].offset + entries[-1].duration) / 3600)
    return p


def current_clip(meta_path: Path | None, out_time_s: float | None) -> dict | None:
    if not meta_path or out_time_s is None:
        return None
    try:
        ents = json.loads(meta_path.read_text(encoding="utf-8"))["entries"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    lo, hi = 0, len(ents) - 1
    if hi < 0:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ents[mid]["offset"] <= out_time_s:
            lo = mid
        else:
            hi = mid - 1
    return ents[lo]
