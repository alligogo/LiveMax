"""관리 프로그램(livemaxd) 순간 CPU 범인 찾기 — 몰릴 때만 '어느 스레드가 무슨 함수를 하고 있었는지' 로그 한 줄.

0.5초마다 /proc/self/task 의 스레드별 CPU 시간을 읽는다 (파일 몇십 개 읽기 — 부담 거의 없음).
0.5초 동안 코어 하나의 40% 이상을 쓰면, CPU 를 가장 많이 쓴 스레드 3개의 이름과 그 순간 실행 중이던
함수(파일:줄)를 남긴다. 로그는 10초에 최대 1줄. 송출(ffmpeg·중계기·Liquidsoap)은 별도 프로세스라 대상 아님.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback

log = logging.getLogger("livemax.cpuwatch")


def _thread_ticks() -> dict[int, int]:
    out = {}
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/stat") as f:
                rest = f.read().rsplit(")", 1)[1].split()
            out[int(tid)] = int(rest[11]) + int(rest[12])  # utime + stime
        except (OSError, IndexError, ValueError):
            continue
    return out


def _where(ident: int | None, frames: dict) -> str:
    fr = frames.get(ident) if ident is not None else None
    if fr is None:
        return "?"
    st = [f for f in traceback.extract_stack(fr) if "/livemax/" in f.filename] or traceback.extract_stack(fr)
    return " ← ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(st[-3:]))


def run(stop: threading.Event, interval: float = 0.5, threshold: float = 0.4, quiet_s: float = 10.0) -> None:
    if not os.path.isdir("/proc/self/task"):
        return
    tck = os.sysconf("SC_CLK_TCK")
    prev, last_log = _thread_ticks(), 0.0
    while not stop.wait(interval):
        cur = _thread_ticks()
        used = (sum(cur.values()) - sum(prev.get(t, 0) for t in cur)) / tck / interval
        if used >= threshold and time.monotonic() - last_log >= quiet_s:
            last_log = time.monotonic()
            frames = sys._current_frames()  # noqa: SLF001 — 진단 목적
            by_native = {th.native_id: th for th in threading.enumerate()}
            top = sorted(((cur[t] - prev.get(t, 0), t) for t in cur), reverse=True)[:3]
            parts = []
            for d, tid in top:
                if d <= 0:
                    continue
                th = by_native.get(tid)
                pct = d / tck / interval * 100
                parts.append(f"{th.name if th else tid} {pct:.0f}% @ {_where(th.ident if th else None, frames)}")
            log.warning("관리 프로그램 순간 CPU %d%% — %s", used * 100, " | ".join(parts))
        prev = cur
