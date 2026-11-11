"""자동 계산 지표 (DESIGN 7.3, 11-D) — "안전하게 몇 개 더 추가 가능".

서버마다 벽이 다르다. live1 실측:
  - Contabo (트래픽 월 32TB): 트래픽이 먼저 막힘
  - OVH VPS-2 (트래픽 무제한, 1Gbps 보장): **메모리**가 먼저 막힘 (라이브당 약 150MB, CPU 는 라이브당 약 0.5%)
그래서 메모리·CPU·회선·(한도가 있으면) 월 트래픽을 모두 보고 가장 먼저 막히는 것으로 낸다.

라이브 1개 몫은 이 서버에서 실제로 잰 값을 쓴다 (서버 사양·업체마다 다르므로). 송출 중인 라이브가 없으면 기본값.
안전 기준 (합의): 메모리 여유 700MB 남김 · CPU 평균 50% · 회선 80% · 트래픽은 목표선(한계의 87.5%)까지.
"""

from __future__ import annotations

import calendar
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

TB = 1e12


@dataclass
class Capacity:
    can_add: int
    limiting: str  # ram | cpu | bandwidth | traffic | max
    per_live_mbps: float
    per_live_tb: float
    mem_per_live_mb: float
    cpu_per_live_pct: float  # 서버 전체(모든 코어) 대비 %
    port_mbit: float
    traffic_unlimited: bool
    month_tx_tb: float
    projected_tb: float
    limit_tb: float  # 0 = 무제한
    target_tb: float
    usage_ratio: float  # 가장 먼저 막히는 자원을 얼마나 썼나 (0~1+, 막대 표시용)
    options: dict = field(default_factory=dict)  # 자원별 "몇 개 더"
    usage: dict = field(default_factory=dict)  # 자원별 사용 비율 (안전 기준 대비)
    rooms: dict = field(default_factory=dict)  # 자원별 안전선까지 남은 양 (MB · CPU % · Mbps · TB) — 해상도별 계산용
    cores: int = 1
    month_seconds: float = 30 * 86400.0


def month_fraction(now: datetime) -> tuple[float, float]:
    """(이번 달 경과 초, 이번 달 전체 초) — UTC 기준 (vnstat 도 서버 시계 = UTC)."""
    days = calendar.monthrange(now.year, now.month)[1]
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (now - start).total_seconds(), days * 86400.0


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def compute(cfg: dict, live_bitrates_kbps: list[int], month_tx_bytes: float | None, current_tx_mbps: float | None,
            cpu_used_pct: float | None, mem_used_mb: float | None, mem_total_mb: float | None,
            now: datetime | None = None, live_cpu: list[float] | None = None, running: int | None = None,
            cores: int | None = None) -> Capacity:
    """live_bitrates_kbps: 켜 둔 라이브들의 영상 비트레이트. running: 그중 실제 송출 중인 수 (실측 나눗셈용).
    live_cpu: 송출 중인 라이브별 CPU (코어 1개 대비 %, ffmpeg+Liquidsoap+중계기). cpu_used_pct: 서버 전체 CPU %."""
    now = now or datetime.now(timezone.utc)
    elapsed, total = month_fraction(now)
    n = len(live_bitrates_kbps)
    running = n if running is None else max(0, min(running, n))
    cores = max(1, int(cores or os.cpu_count() or 1))

    # --- 라이브 1개가 쓰는 회선·트래픽
    audio = _f(cfg, "audio_kbps", 128)
    overhead = 1 + _f(cfg, "overhead_ratio", 0.03)
    avg_kbps = (sum(live_bitrates_kbps) / n) if n else 5000.0
    per_live_mbps = (avg_kbps + audio) * overhead / 1000
    per_live_tb = per_live_mbps * 1e6 / 8 * total / TB
    options: dict[str, int] = {}
    usage: dict[str, float] = {}
    rooms: dict[str, float] = {}

    # --- 메모리: 여유 mem_reserve_mb 를 남기고. 라이브당 = (지금 사용 − 기본) ÷ 송출 중 라이브 수
    if mem_used_mb is not None and mem_total_mb:
        reserve = _f(cfg, "mem_reserve_mb", 700)
        base = _f(cfg, "mem_base_mb", 550)
        per_mem = (mem_used_mb - base) / running if running else _f(cfg, "mem_per_live_mb", 150)
        per_mem = min(max(per_mem, 60.0), 1000.0)
        pending = (n - running) * per_mem  # 켜 두었지만 아직 안 올라온 라이브 몫
        room = mem_total_mb - reserve - mem_used_mb - pending
        options["ram"] = int(room // per_mem)
        rooms["ram"] = room
        usage["ram"] = (mem_used_mb + pending) / max(1.0, mem_total_mb - reserve)
    else:
        per_mem = _f(cfg, "mem_per_live_mb", 150)

    # --- CPU: 평균 cpu_target_pct 까지. 라이브 몫은 라이브 프로그램이 실제로 쓴 CPU 평균
    #     (서버 전체를 라이브 수로 나누면 업로드 검사·썸네일 같은 일시 작업까지 라이브 몫이 되어 과대 계산된다)
    measured = [c for c in (live_cpu or []) if c and c > 0]
    per_core = max(sum(measured) / len(measured), 0.5) if measured else _f(cfg, "cpu_per_live_core_pct", 2.0)
    cpu_per_live_pct = per_core / cores  # 서버 전체 대비
    if cpu_used_pct is not None:
        target = _f(cfg, "cpu_target_pct", 50)
        pending = (n - running) * cpu_per_live_pct
        options["cpu"] = int((target - cpu_used_pct - pending) // cpu_per_live_pct) if cpu_per_live_pct else 0
        rooms["cpu"] = target - cpu_used_pct - pending
        usage["cpu"] = (cpu_used_pct + pending) / max(1.0, target)

    # --- 회선: 보장 속도의 port_use_ratio 까지
    port = _f(cfg, "port_mbit", 1000)
    ratio = _f(cfg, "port_use_ratio", 0.5)
    cur = max(current_tx_mbps or 0.0, n * per_live_mbps)
    if port > 0:
        options["bandwidth"] = int((port * ratio - cur) // per_live_mbps) if per_live_mbps else 0
        rooms["bandwidth"] = port * ratio - cur
        usage["bandwidth"] = cur / (port * ratio)

    # --- 월 트래픽: 한도(TB)가 있을 때만. 0 = 무제한
    limit_tb = _f(cfg, "monthly_traffic_tb", 0)
    month_tx_tb = (month_tx_bytes or 0) / TB
    trend_tb = month_tx_tb / elapsed * total if elapsed > 3600 and month_tx_bytes else 0.0
    projected_tb = max(n * per_live_tb, trend_tb)  # 설정 기준 예상치와 실측 추세 중 큰 값 (보수적)
    target_tb = 0.0
    if limit_tb > 0:
        target_tb = _f(cfg, "traffic_target_tb", 0) or limit_tb * 0.875
        options["traffic"] = int((target_tb - projected_tb) // per_live_tb) if per_live_tb else 0
        rooms["traffic"] = target_tb - projected_tb
        usage["traffic"] = projected_tb / limit_tb

    if not options:
        options["bandwidth"] = 0
    limiting = min(options, key=lambda k: options[k])
    return Capacity(
        can_add=max(0, options[limiting]), limiting=limiting,
        per_live_mbps=per_live_mbps, per_live_tb=per_live_tb,
        mem_per_live_mb=per_mem, cpu_per_live_pct=cpu_per_live_pct,
        port_mbit=port, traffic_unlimited=limit_tb <= 0,
        month_tx_tb=month_tx_tb, projected_tb=projected_tb, limit_tb=limit_tb, target_tb=target_tb,
        usage_ratio=usage.get(limiting, 0.0), options=options, usage=usage,
        rooms=rooms, cores=cores, month_seconds=total,
    )


def add_by_profile(cfg: dict, cap: Capacity, video_kbps: float, audio_kbps: float, extra_mem_mb: float = 0.0,
                   cpu_audio_core: float | None = None, cpu_video_core_per_mbps: float | None = None) -> tuple[int, str] | None:
    """이 비트레이트의 라이브를 '몇 개 더' 올릴 수 있나 (v2.17 — 서버 박스 '1080p N개 / 4K M개', 둘 중 하나로 다 쓸 때).

    라이브 1개 몫을 자원별로 다시 계산해 안전선까지 남은 양(cap.rooms)을 나눈다:
      - 회선·트래픽: (영상 + 소리) × (1 + 오버헤드)
      - 메모리: 실측 라이브당 값 + extra_mem_mb (4K 버퍼 여유)
      - CPU: 음악 몫(비트레이트 무관) + 영상 몫(ffmpeg·중계기 — 1Mbps 당 실측 × 이 비트레이트). 데이터 양에 비례한다고 보는
        가장 나쁜 가정이라 4K 는 보수적으로 나오고, 4K 를 실제로 돌리면 실측이 섞여 저절로 정확해진다.
        나눠 잰 값이 없으면(송출 중 라이브 0) 라이브당 CPU 를 비트레이트 비율로 늘린다."""
    overhead = 1 + _f(cfg, "overhead_ratio", 0.03)
    mbps = (float(video_kbps) + float(audio_kbps)) * overhead / 1000
    if mbps <= 0:
        return None
    costs: dict[str, float] = {}
    if "ram" in cap.rooms:
        costs["ram"] = cap.mem_per_live_mb + float(extra_mem_mb)
    if "cpu" in cap.rooms:
        if cpu_audio_core is not None and cpu_video_core_per_mbps is not None:
            core = cpu_audio_core + cpu_video_core_per_mbps * mbps
        else:
            core = cap.cpu_per_live_pct * cap.cores * max(1.0, mbps / cap.per_live_mbps if cap.per_live_mbps else 1.0)
        costs["cpu"] = max(core, 0.05) / max(1, cap.cores)
    if "bandwidth" in cap.rooms:
        costs["bandwidth"] = mbps
    if "traffic" in cap.rooms:
        costs["traffic"] = mbps * 1e6 / 8 * cap.month_seconds / TB
    n = {k: int(cap.rooms[k] // c) for k, c in costs.items() if c > 0}
    if not n:
        return None
    k = min(n, key=lambda x: n[x])
    return max(0, n[k]), k


SHORT_BY = {"ram": "메모리", "cpu": "CPU", "bandwidth": "회선", "traffic": "월 트래픽"}


def max_guess(cap: Capacity, lives: int) -> tuple[int, str] | None:
    """'예상 최대 N개 (자원)' = 지금 라이브 수 + 안전선 기준 가장 먼저 막히는 자원의 '몇 개 더' (최대 개수 설정은 빼고).
    화면(app.js maxGuess)과 같은 계산."""
    opts = [(v, k) for k, v in cap.options.items() if k in SHORT_BY]
    if not opts:
        return None
    v, k = min(opts)
    return lives + max(0, v), k


class DayLow:
    """'예상 최대'의 오늘 최솟값 (DESIGN 11-D 20 · v2.14) — 하루 중 가장 나빴던 15초 동안 몇 개까지였나.
    수집기(15초)가 이미 잰 값으로 계산만 하므로 부담 없음. 켜 둔 라이브가 모두 송출 중일 때만 센다
    (켜는 중·재시작 중의 순간 피크는 정상 과정이라 빼야 숫자가 쓸모 있다). 기억만 하므로 업데이트하면 다시 셈."""

    def __init__(self):
        self.day = None
        self.low: tuple[int, str, float] | None = None  # (개수, 자원, 시각)

    def update(self, day: float, guess: tuple[int, str] | None, now: float) -> None:
        if day != self.day:
            self.day, self.low = day, None
        if guess and (self.low is None or guess[0] < self.low[0]):
            self.low = (guess[0], guess[1], now)

    def snapshot(self) -> dict:
        if not self.low:
            return {}
        return {"guess_low": self.low[0], "guess_low_by": self.low[1], "guess_low_at": self.low[2]}
