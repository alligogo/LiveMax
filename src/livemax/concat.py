"""영상 목록 — 24시간치 concat 목록 사전 생성 (DESIGN 4.4, v2 단순화).

선택한 영상 폴더들의 클립을 셔플백(한 바퀴 돌기 전엔 중복 없음)으로 섞어 24시간+여유만큼 채운다.
ffmpeg 는 목록을 순서대로 copy 재생할 뿐이다. 송출 시작(재시작)할 때마다 새로 만든다.
목록 옆 .json 메타(각 항목 시작 오프셋)로 "지금 나가는 클립"을 계산한다 (서버 캡처 없이).
"""

from __future__ import annotations

from array import array
from bisect import bisect_right

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
    t = 0.0
    if getattr(live, "video_order", "sequential") != "random":
        # 이름순(1, 2, … 9, 10 — 자연 정렬, 고른 폴더 순서대로) 반복 — 이름으로 순서를 짤 수 있게
        i = 0
        while t < total:
            clip = usable[i % len(usable)]
            entries.append(Entry(clip, durations[clip], t))
            t += durations[clip]
            i += 1
        return entries
    bag: list[Path] = []
    last: Path | None = None
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


# 메타 캐시: 클립 수천 개를 dict 수천 개로 들고 있으면 파이썬 메모리 청소(GC)가 훑을 묶음이 수만 개가 된다
# (라이브 10개 × 3,300개). 시작 시각·길이는 숫자 배열, 경로는 문자열 목록으로만 들고 있는다 (청소 대상 거의 없음).
_META_CACHE: dict[Path, tuple[int, array, array, tuple[str, ...]]] = {}


def current_clip(meta_path: Path | None, out_time_s: float | None) -> dict | None:
    if not meta_path or out_time_s is None:
        return None
    try:
        mt = meta_path.stat().st_mtime_ns
        hit = _META_CACHE.get(meta_path)
        if not hit or hit[0] != mt:  # 2초마다 × 라이브 수만큼 불리므로 파일이 바뀌었을 때만 다시 읽는다
            ents = json.loads(meta_path.read_text(encoding="utf-8"))["entries"]
            hit = (mt, array("d", (float(e["offset"]) for e in ents)), array("d", (float(e["duration"]) for e in ents)),
                   tuple(str(e["path"]) for e in ents))
            del ents
            for k in [k for k in list(_META_CACHE) if k.parent == meta_path.parent and k != meta_path]:
                _META_CACHE.pop(k, None)  # 여러 스레드가 같이 부르므로 복사본으로 돌고, 없으면 넘어간다  # 같은 라이브의 지난 목록은 버린다 (라이브마다 폴더가 따로)
            _META_CACHE[meta_path] = hit
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    _, offs, durs, paths_ = hit
    if not offs:
        return None
    i = max(0, bisect_right(offs, out_time_s) - 1)
    return {"path": paths_[i], "offset": offs[i], "duration": durs[i]}
