"""/proc 읽기만으로 시스템·프로세스 지표 수집 (DESIGN 7.1, 7.2). 부하는 무시할 수준.

steal time 이 Contabo 공유 vCPU 에서 가장 중요한 지표다: 다른 VM 이 CPU 를 가져간 비율.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


@dataclass
class CpuTimes:
    total: int
    idle: int
    iowait: int
    steal: int


def read_cpu() -> CpuTimes:
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    # user nice system idle iowait irq softirq steal guest guest_nice
    vals += [0] * (10 - len(vals))
    user, nice, system, idle, iowait, irq, softirq, steal = vals[:8]
    # guest 는 user 에 이미 포함되어 있으므로 total 에 더하지 않는다
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return CpuTimes(total, idle, iowait, steal)


def cpu_percent(prev: CpuTimes, cur: CpuTimes) -> tuple[float, float, float]:
    """(사용률%, steal%, iowait%) — 전체 코어 합산 기준 0~100."""
    dt = cur.total - prev.total
    if dt <= 0:
        return 0.0, 0.0, 0.0
    busy = dt - (cur.idle - prev.idle) - (cur.iowait - prev.iowait)
    return (100.0 * busy / dt, 100.0 * (cur.steal - prev.steal) / dt, 100.0 * (cur.iowait - prev.iowait) / dt)


def read_mem() -> tuple[float, float]:
    """(사용 MB, 전체 MB) — MemAvailable 기준."""
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
    total = info.get("MemTotal", 0) / 1024
    avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024
    return total - avail, total


def disk_usage(path: Path) -> tuple[float, float, float]:
    """(사용 GB, 전체 GB, 사용률%)"""
    u = shutil.disk_usage(path)
    return u.used / 1e9, u.total / 1e9, 100.0 * u.used / u.total if u.total else 0.0


def default_iface() -> str | None:
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if parts[1] == "00000000":
                    return parts[0]
    except (OSError, StopIteration, IndexError):
        pass
    return None


def read_net(iface: str) -> tuple[int, int]:
    """(rx_bytes, tx_bytes) 누적값"""
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() == iface:
                p = rest.split()
                return int(p[0]), int(p[8])
    raise KeyError(iface)


def vnstat_month_tx_bytes(iface: str) -> int | None:
    """vnstat 의 이번 달 누적 송신량 (FUP 감시). vnstat 2.x JSON 형식."""
    exe = shutil.which("vnstat")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--json", "m", "1", "-i", iface], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
        months = data["interfaces"][0]["traffic"]["month"]
        return int(months[-1]["tx"]) if months else 0
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, IndexError, ValueError):
        return None


def proc_cpu_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        fields = data[data.rindex(")") + 2:].split()
        return int(fields[11]) + int(fields[12])  # utime + stime
    except (OSError, ValueError, IndexError):
        return None


def proc_rss_mb(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * PAGE / 1e6
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid: int | None) -> bool:
    return bool(pid) and Path(f"/proc/{pid}").exists()
