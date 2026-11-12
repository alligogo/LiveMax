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


def read_swap() -> tuple[float, float]:
    """(스왑 사용 MB, 스왑 전체 MB). 스왑이 없으면 (0, 0)."""
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
    except OSError:
        return 0.0, 0.0
    total = info.get("SwapTotal", 0) / 1024
    return total - info.get("SwapFree", 0) / 1024, total


def read_swap_io() -> int:
    """지금까지 스왑으로 들어가고 나온 페이지 수 합 (pswpin + pswpout). 두 번 읽은 차이 ÷ 시간 = 스왑 활동량."""
    n = 0
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k in ("pswpin", "pswpout"):
                    n += int(v)
    except (OSError, ValueError):
        return 0
    return n


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


class PeakTracker:
    """CPU·메모리의 '오늘 최고값' (DESIGN 11-D 19 · v2.13). 수집기는 15초 평균이라 순간 피크가 안 보인다 —
    1초마다 /proc/stat·/proc/meminfo 두 파일만 읽어 하루(day_start 기준) 최고값과 그 시각만 기억한다 (부담 거의 없음).
    실측(라이브 40개): 15초 평균 약 24% 일 때 1초 최고 43% — 서버 업체 그래프가 이 1초 최고값과 비슷하게 나온다."""

    def __init__(self, day_start=None):
        self._day_start = day_start or (lambda now: 0.0)
        self._prev: CpuTimes | None = None
        self._day = None
        self.cpu = (0.0, 0.0)  # (최고 %, 시각)
        self.mem = (0.0, 0.0)  # (최고 MB, 시각)

    def sample(self, now: float) -> None:
        day = self._day_start(now)
        if day != self._day:
            self._day, self.cpu, self.mem = day, (0.0, 0.0), (0.0, 0.0)
        cur = read_cpu()
        if self._prev is not None:
            pct = cpu_percent(self._prev, cur)[0]
            if pct > self.cpu[0]:
                self.cpu = (pct, now)
        self._prev = cur
        used = read_mem()[0] + read_swap()[0]  # 스왑으로 옮겨진 몫도 쓰는 메모리로 (서버 박스와 같은 기준)
        if used > self.mem[0]:
            self.mem = (used, now)

    def snapshot(self) -> dict:
        return {"cpu_peak_pct": round(self.cpu[0], 1), "cpu_peak_at": self.cpu[1] or None,
                "mem_peak_mb": round(self.mem[0]), "mem_peak_at": self.mem[1] or None}

    def run(self, stop) -> None:
        import time

        while not stop.wait(1):
            try:
                self.sample(time.time())
            except Exception:  # noqa: BLE001 — 측정 실패가 다른 일을 막지 않게
                pass


def read_tcp_counters() -> tuple[int, int] | None:
    """(보낸 TCP 조각 누적, 다시 보낸 조각 누적) — /proc/net/snmp. 재전송률이 오르면 서버→유튜브 경로가 붐비는 것 (v2.19)."""
    try:
        with open("/proc/net/snmp") as f:
            rows = [ln.split() for ln in f if ln.startswith("Tcp:")]
        if len(rows) < 2:
            return None
        d = dict(zip(rows[0][1:], rows[1][1:]))
        return int(d["OutSegs"]), int(d["RetransSegs"])
    except (OSError, KeyError, ValueError):
        return None
