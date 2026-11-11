"""지표 수집 (DESIGN 7) — /proc 읽기와 커널 카운터뿐 (CPU 0.5% 미만).

latest 에 최신 값을 들고 있어 웹 API 가 바로 읽고, SQLite 시계열에도 적재한다.
디스크 90% 초과 시 긴급 정리를 요청한다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from . import capacity, config, db, sysmetrics
from .paths import Paths

log = logging.getLogger("livemax.collector")


class Collector:
    def __init__(self, paths: Paths, settings: dict, supervisor, on_emergency=None):
        self.paths, self.settings, self.sup = paths, settings, supervisor
        self.on_emergency = on_emergency
        self.latest: dict = {}
        self.per_live: dict[str, dict] = {}
        self._prev_proc: dict[int, int] = {}
        self.day_low = capacity.DayLow()  # '예상 최대'의 오늘 최솟값 (서버 박스 '최소기준')
        self._low_win: deque = deque()  # 최소기준용 최근 5분 표본 (시각, CPU, 메모리, 송신)

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
                # 음악 묶음 모드: 한 Liquidsoap 을 여러 라이브가 같이 쓴다 → 그 몫을 쓰는 라이브 수로 나눠 붙인다
                share: dict[int, int] = {}
                for s_ in snap.values():
                    for x in (s_.get("video_pid"), s_.get("audio_pid"), s_.get("relay_pid")):
                        if x:
                            share[x] = share.get(x, 0) + 1
                cpu_of: dict[int, float] = {}
                for lid, s in snap.items():
                    if s.get("state") == "running":
                        running += 1
                    pids = [x for x in (s.get("video_pid"), s.get("audio_pid"), s.get("relay_pid")) if x]
                    for x in pids:
                        if x not in cpu_of:
                            cpu_of[x] = self._cpu(x, dt, 0)  # 같은 pid 를 두 번 재면 두 번째가 0 이 된다
                    vp = [x for x in (s.get("video_pid"), s.get("relay_pid")) if x]
                    ap = s.get("audio_pid")
                    self.per_live[lid] = {
                        "cpu": sum(cpu_of[x] / share[x] for x in pids),
                        # v2.17: 음악 몫(묶음이면 나눈 몫)과 영상 몫(ffmpeg + 중계기) — 해상도별 '추가 가능' 계산용
                        "cpu_a": (cpu_of[ap] / share[ap]) if ap else 0.0,
                        "cpu_v": sum(cpu_of[x] / share[x] for x in vp),
                        "rss_mb": sum((sysmetrics.proc_rss_mb(x) or 0) / share[x] for x in pids) or None,
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
                self._track_low(now, snap, cpu, mem_used + (swap_used or 0), mem_total, tx, month_tx, running)
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

    LOW_WINDOW_S = 300  # 최소기준은 5분 평균 (v2.17) — 매일 1분 도는 우분투 보안 점검 같은 짧은 피크가 하루 숫자를 대표하지 않게
    LOW_SETTLE_S = 300  # 라이브를 켜거나 재시작한 뒤 이만큼은 세지 않는다 (업데이트·추가·재시작 직후 마무리 부하)

    def _track_low(self, now, snap, cpu, mem_used, mem_total, tx, month_tx, running) -> None:
        """서버 박스 '최소기준 N개 (하루)' — 화면이 꺼져 있는 새벽에도 세도록 수집기에서 (계산만, 부담 없음).
        순간 피크는 'CPU 오늘 최고'(1초)가 보여 주므로, 여기선 '오래가는 나쁜 조건'(새벽 steal 수십 분, 꾸준한 상승)만."""
        try:
            planned = [lid for lid in (lv.id for lv in self.sup.store.all() if lv.enabled)]
            win = self._low_win
            unsettled = not planned or any(
                (snap.get(lid) or {}).get("state") != "running"
                or now - float((snap.get(lid) or {}).get("ffmpeg_started_at") or 0) < self.LOW_SETTLE_S for lid in planned)
            if unsettled:
                win.clear()  # 켜는 중·재시작 중이거나 켠 지 5분 안 됨 → 그 앞뒤 표본은 섞지 않는다
                return
            win.append((now, cpu, mem_used, tx))
            while win and now - win[0][0] > self.LOW_WINDOW_S:
                win.popleft()
            if now - win[0][0] < self.LOW_WINDOW_S - 20:  # 5분이 다 차야 센다
                return
            n = len(win)
            avg = lambda i: sum((x[i] or 0) for x in win) / n  # noqa: E731
            audio = float(self.settings["capacity"].get("audio_kbps", 128))
            kbps = [max(500.0, float(snap[lid].get("kbps_in") or 5128) - audio) for lid in planned]
            cap = capacity.compute(self.settings["capacity"], kbps, month_tx, avg(3), avg(1), avg(2), mem_total,
                                   live_cpu=[self.per_live.get(lid, {}).get("cpu") for lid in planned], running=running)
            self.day_low.update(config.day_start(self.settings, now), capacity.max_guess(cap, len(planned)), now)
        except Exception:  # noqa: BLE001 — 표시용 숫자가 수집을 막지 않게
            log.debug("최소기준 계산 실패", exc_info=True)

    def _cpu(self, pid, dt, default):
        if not sysmetrics.pid_alive(pid):
            return default
        ticks = sysmetrics.proc_cpu_ticks(pid)
        before = self._prev_proc.get(pid)
        self._prev_proc[pid] = ticks
        if ticks is None or before is None:
            return default
        return 100.0 * (ticks - before) / sysmetrics.CLK_TCK / dt
