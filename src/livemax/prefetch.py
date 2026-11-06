"""다음 곡·다음 영상 미리 읽어 두기 — 디스크가 순간 느려져도 송출이 멈칫하지 않게.

송출 중에 음악 프로그램(Liquidsoap)·ffmpeg 가 파일을 그때그때 디스크에서 읽는다.
서버 디스크가 1~2초 느려지면(밤새 'We must catchup' 이 여러 라이브에서 같은 분에 찍힌 원인으로 추정)
소리·영상이 멈칫하고 유튜브는 그동안 아무것도 못 받는다.
→ 곧 재생될 파일을 커널에 '미리 읽어 두라'(posix_fadvise WILLNEED)고 알려 메모리(페이지 캐시)에 올려 둔다.
   복사·디코딩이 없어 CPU 는 거의 안 쓰고, 읽기는 커널이 뒤에서 한다. 메모리가 모자라면 커널이 알아서 비운다.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from . import concat
from .state import read_json

log = logging.getLogger("livemax.prefetch")

LOOKAHEAD_S = 90.0  # 지금 영상 다음으로 이만큼(초) 안에 나올 영상들을 미리 읽는다 (짧은 클립이면 여러 개)
MAX_FILES = 6


def warm(path: str | Path) -> bool:
    """파일을 메모리에 미리 올려 달라고 커널에 요청 (기다리지 않음)."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_WILLNEED)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def upcoming_clips(meta_path: Path | None, out_time_s: float | None) -> list[str]:
    """지금 나가는 영상 + 앞으로 LOOKAHEAD_S 안에 나올 영상들 (목록 순서대로, 중복 없이)."""
    cur = concat.current_clip(meta_path, out_time_s)
    if not cur:
        return []
    hit = concat._META_CACHE.get(meta_path)  # noqa: SLF001 — current_clip 이 방금 채운 캐시
    if not hit:
        return [cur["path"]]
    _, offs, durs, paths_ = hit
    i = max(0, concat.bisect_right(offs, out_time_s) - 1)
    out: list[str] = []
    end = (out_time_s or 0) + LOOKAHEAD_S
    first = i
    while i < len(paths_) and len(out) < MAX_FILES and (i == first or offs[i] <= end):
        if paths_[i] not in out:
            out.append(paths_[i])
        i += 1
    return out


def files_for(proc) -> list[str]:
    """라이브 하나가 곧 쓸 파일들: 지금·다음 곡, 지금·다음 영상."""
    run = proc.sup.paths.run_dir
    files = []
    for name in (f"{proc.id}.nowplaying.json", f"{proc.id}.next.json"):
        f = (read_json(run / name) or {}).get("file")
        if f:
            files.append(str(f))
    with proc.lock:
        ot = proc.st.get("out_time")
    files += upcoming_clips(proc.meta_path, ot)
    return files


def run(sup, stop: threading.Event, interval: float = 10.0) -> None:
    """송출 중인 라이브들의 곧 쓸 파일을 주기적으로 미리 읽어 둔다. 같은 파일은 다시 요청하지 않는다."""
    done: dict[str, set[str]] = {}
    while not stop.wait(interval):
        try:
            for lid, p in list(sup.procs.items()):
                if not p.desired:
                    done.pop(lid, None)
                    continue
                want = files_for(p)
                seen = done.setdefault(lid, set())
                for f in want:
                    if f not in seen and warm(f):
                        seen.add(f)
                if len(seen) > 50:  # 오래된 기록은 버린다 (한 바퀴 돌아 다시 나오면 또 미리 읽게)
                    done[lid] = set(want)
        except Exception:  # noqa: BLE001
            log.exception("미리 읽기 실패")
