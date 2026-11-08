"""지표 수집 (DESIGN 7) — /proc 읽기와 커널 카운터뿐 (CPU 0.5% 미만).

latest 에 최신 값을 들고 있어 웹 API 가 바로 읽고, SQLite 시계열에도 적재한다.
디스크 90% 초과 시 긴급 정리를 요청한다.
"""

from __future__ import annotations

import logging
import threading
import time

from . import db, sysmetrics
from .paths import Paths

log = logging.getLogger("livemax.collector")


class Collector:
    def __init__(self, paths: Paths, settings: dict, supervisor, on_emergency=None):
        self.paths, self.settings, self.sup = paths, settings, supervisor
        self.on_emergency = on_emergency
        self.latest: dict = {}
        self.per_live: dict[str, dict] = {}
        self._prev_proc: dict[int, int] = {}

    def run(self, stop: threading.Event) -> None:
        iface = sysmetrics.default_iface()
        prev_cpu = sysmetrics.read_cpu()
        prev_net = sysmetrics.read_net(iface) if iface else (0, 0)
        prev_t = time.monotonic()
        month_tx, last_vn, last_emerg = None, 0.0, 0.0
        conn = db.connect(self.paths.db)
        interval = float(self.settings["collector"]["interval_seconds"])
        while not stop.wait(interval):
            try:
                now, t = time.time(), time.monotonic()
                dt = max(t - prev_t, 0.001)
                cur = sysmetrics.read_cpu()
                cpu, steal, iowait = sysmetrics.cpu_percent(prev_cpu, cur)
                prev_cpu = cur
                mem_used, mem_total = sysmetrics.read_mem()
                swap_used, swap_total = sysmetrics.read_swap()
                d_used, d_total, d_pct = sysmetrics.disk_usage(self.paths.root)
                tx = rx = None
                if iface:
                    r, x = sysmetrics.read_net(iface)
                    rx, tx = (r - prev_net[0]) * 8 / dt / 1e6, (x - prev_net[1]) * 8 / dt / 1e6
                    prev_net = (r, x)
                prev_t = t
                if iface and now - last_vn >= float(self.settings["collector"]["vnstat_interval_seconds"]):
                    last_vn = now
                    v = sysmetrics.vnstat_month_tx_bytes(iface)
                    if v is not None:
                        month_tx = v
                # 라이브별 CPU·RAM
                snap = self.sup.snapshot()
                running = 0
                live_rows = []
                for lid, s in snap.items():
                    if s.get("state") == "running":
                        running += 1
                    pids = [x for x in (s.get("video_pid"), s.get("audio_pid"), s.get("relay_pid")) if x]
                    self.per_live[lid] = {
                        "cpu": sum(self._cpu(x, dt, 0) for x in pids),
                        "rss_mb": sum(sysmetrics.proc_rss_mb(x) or 0 for x in pids) or None,
                    }
                    fresh = now - (s.get("relay_at") or 0) < 40  # 중계기 알림이 최근 것일 때만 (꺼진 라이브의 옛 값 X)
                    live_rows.append((now, lid, s.get("state"), s.get("speed"), self.per_live[lid]["cpu"],
                                      self.per_live[lid]["rss_mb"], s.get("out_time"),
                                      s.get("kbps_in") if fresh else None, s.get("kbps_out") if fresh else None,
                                      s.get("delay_s") if fresh else None))
                self.latest = {"ts": now, "cpu_pct": cpu, "steal_pct": steal, "iowait_pct": iowait,
                               "mem_used_mb": mem_used, "mem_total_mb": mem_total, "disk_used_gb": d_used,
                               "swap_used_mb": swap_used, "swap_total_mb": swap_total,
                               "disk_total_gb": d_total, "disk_pct": d_pct, "net_tx_mbps": tx, "net_rx_mbps": rx,
                               "month_tx_bytes": month_tx, "lives_running": running, "iface": iface}
                # 한 번의 짧은 트랜잭션으로 (라이브 15개면 예전엔 쓰기 16번)
                conn.execute("BEGIN")
                conn.executemany("INSERT INTO metrics_live(ts, live_id, state, speed, cpu_pct, rss_mb, out_time_s,"
                                 " kbps_in, kbps_out, delay_s) VALUES (?,?,?,?,?,?,?,?,?,?)", live_rows)
                conn.execute(
                    "INSERT INTO metrics_system(ts, cpu_pct, steal_pct, iowait_pct, mem_used_mb, mem_total_mb, disk_used_gb,"
                    " disk_total_gb, net_tx_mbps, net_rx_mbps, month_tx_gb, lives_running) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, cpu, steal, iowait, mem_used, mem_total, d_used, d_total, tx, rx,
                     month_tx / 1e9 if month_tx is not None else None, running))
                conn.execute("COMMIT")
                if d_pct >= float(self.settings["cleanup"]["disk_emergency_percent"]) and now - last_emerg > 3600:
                    last_emerg = now
                    db.add_event(conn, "error", "collector", f"디스크 {d_pct:.1f}% — 긴급 정리 실행")
                    if self.on_emergency:
                        self.on_emergency()
            except Exception:  # noqa: BLE001
                log.exception("수집 실패")
                if conn.in_transaction:  # 열린 트랜잭션이 잠금을 쥔 채 남지 않게
                    conn.execute("ROLLBACK")

    def _cpu(self, pid, dt, default):
        if not sysmetrics.pid_alive(pid):
            return default
        ticks = sysmetrics.proc_cpu_ticks(pid)
        before = self._prev_proc.get(pid)
        self._prev_proc[pid] = ticks
        if ticks is None or before is None:
            return default
        return 100.0 * (ticks - before) / sysmetrics.CLK_TCK / dt
