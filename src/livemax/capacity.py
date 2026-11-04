"""자동 계산 지표 (DESIGN 7.3).

- 현재 추세 기준 월 예상 트래픽
- 32TB 추정선 대비 사용률
- "안전하게 몇 개 더 추가 가능" — 사용자가 가장 원하는 단일 숫자

병목은 트래픽 총량 하나뿐이지만(DESIGN 2.3), 대역폭·CPU·RAM 도 함께 보고 가장 작은 값을 낸다.
라이브당 비용은 라이브별 설정 비트레이트에서 계산한다 (전역 상수 아님).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone

TB = 1e12


@dataclass
class Capacity:
    month_tx_tb: float
    projected_tb: float
    limit_tb: float
    usage_ratio: float  # projected / limit
    per_live_tb: float
    per_live_mbps: float
    can_add: int
    limiting: str  # traffic | bandwidth | cpu | ram
    detail: dict


def month_fraction(now: datetime) -> tuple[float, float]:
    """(이번 달 경과 초, 이번 달 전체 초) — UTC 기준 (vnstat 도 서버 시계 = UTC)."""
    days = calendar.monthrange(now.year, now.month)[1]
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (now - start).total_seconds(), days * 86400.0


def compute(cfg: dict, live_bitrates_kbps: list[int], month_tx_bytes: float | None, current_tx_mbps: float | None,
            cpu_used_pct: float | None, mem_used_mb: float | None, mem_total_mb: float | None,
            now: datetime | None = None, live_cpu: list[float] | None = None) -> Capacity:
    now = now or datetime.now(timezone.utc)
    elapsed, total = month_fraction(now)
    safety = float(cfg["safety_ratio"])
    limit_tb = float(cfg["monthly_traffic_tb"])
    overhead = 1 + float(cfg["overhead_ratio"])
    audio = float(cfg["audio_kbps"])

    n = len(live_bitrates_kbps)
    avg_kbps = (sum(live_bitrates_kbps) / n) if n else 5000.0
    per_live_mbps = (avg_kbps + audio) * overhead / 1000
    per_live_tb = per_live_mbps * 1e6 / 8 * total / TB

    # 설정 기준 예상치와 vnstat 실측 추세 중 큰 값을 쓴다 (보수적)
    nominal_tb = n * per_live_tb
    month_tx_tb = (month_tx_bytes or 0) / TB
    trend_tb = month_tx_tb / elapsed * total if elapsed > 3600 and month_tx_bytes else 0.0
    projected_tb = max(nominal_tb, trend_tb)

    options: dict[str, int] = {}
    options["traffic"] = int((limit_tb * safety - projected_tb) // per_live_tb) if per_live_tb else 0
    port = float(cfg["port_mbit"])
    cur_mbps = max(current_tx_mbps or 0.0, n * per_live_mbps)
    options["bandwidth"] = int((port * safety - cur_mbps) // per_live_mbps) if per_live_mbps else 0
    if cpu_used_pct is not None and n:
        cpu_total = float(cfg["cpu_percent_total"])
        used = cpu_used_pct * cpu_total / 100  # 전체 코어 합산 %
        # 라이브 1개 몫은 라이브 프로그램(ffmpeg+Liquidsoap)이 실제로 쓴 CPU 평균으로 잡는다.
        # 서버 전체 CPU 를 라이브 수로 나누면 업로드 검사·썸네일 같은 일시 작업까지 라이브 몫이 되어 과대 계산된다.
        measured = [c for c in (live_cpu or []) if c and c > 0]
        per = max(sum(measured) / len(measured), 1.0) if measured else max(used / n, 1.0)
        options["cpu"] = int((cpu_total * safety - used) // per)
    if mem_used_mb is not None and mem_total_mb:
        per = float(cfg["ram_mb_per_live"])
        options["ram"] = int((mem_total_mb * safety - mem_used_mb) // per)

    limiting = min(options, key=lambda k: options[k])
    can_add = max(0, options[limiting])
    return Capacity(
        month_tx_tb=month_tx_tb,
        projected_tb=projected_tb,
        limit_tb=limit_tb,
        usage_ratio=projected_tb / limit_tb if limit_tb else 0,
        per_live_tb=per_live_tb,
        per_live_mbps=per_live_mbps,
        can_add=can_add,
        limiting=limiting,
        detail={"options": options, "nominal_tb": nominal_tb, "trend_tb": trend_tb, "lives": n, "avg_kbps": avg_kbps},
    )
