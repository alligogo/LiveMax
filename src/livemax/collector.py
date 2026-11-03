"""지표 수집기 — livemax-collector.service (DESIGN 7)

기본 10초 주기. /proc 읽기와 커널 카운터 조회뿐이라 송출에 영향을 주지 않는다 (CPU 0.5% 미만).
결과는 SQLite 시계열(metrics_system, metrics_live)로 적재한다.
"라이브 3개일 때 vs 10개일 때 steal time" 같은 비교가 확장 판단의 근거가 된다.

디스크 사용률이 emergency 임계(90%)를 넘으면 긴급 정리를 즉시 실행한다 (DESIGN 6.4).
"""

from __future__ import annotations

import logging
import signal
import time

from . import config, db, logsetup, sysmetrics, systemctl
from .state import read_json, write_json_atomic

log = logging.getLogger("livemax.collector")


def main() -> int:
    server = config.load_server()
    paths = config.get_paths(server)
    logsetup.setup("collector", server)
    db.init(paths.db)
    conn = db.connect(paths.db)

    stop = False

    def _sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    iface_cfg = server["collector"]["network_interface"]
    iface = sysmetrics.default_iface() if iface_cfg == "auto" else iface_cfg
    log.info("collector 시작 (iface=%s, interval=%ss)", iface, server["collector"]["interval_seconds"])

    prev_cpu = sysmetrics.read_cpu()
    prev_net = sysmetrics.read_net(iface) if iface else (0, 0)
    prev_t = time.monotonic()
    prev_proc: dict[int, int] = {}
    month_tx: int | None = None
    last_vnstat = 0.0
    last_emergency = 0.0

    while not stop:
        interval = float(server["collector"]["interval_seconds"])
        for _ in range(int(interval * 10)):
            if stop:
                break
            time.sleep(0.1)
        if stop:
            break
        try:
            now = time.time()
            t = time.monotonic()
            dt = max(t - prev_t, 0.001)

            cur_cpu = sysmetrics.read_cpu()
            cpu, steal, iowait = sysmetrics.cpu_percent(prev_cpu, cur_cpu)
            prev_cpu = cur_cpu

            mem_used, mem_total = sysmetrics.read_mem()
            d_used, d_total, d_pct = sysmetrics.disk_usage(paths.media_root)

            tx_mbps = rx_mbps = None
            if iface:
                rx, tx = sysmetrics.read_net(iface)
                rx_mbps = (rx - prev_net[0]) * 8 / dt / 1e6
                tx_mbps = (tx - prev_net[1]) * 8 / dt / 1e6
                prev_net = (rx, tx)
            prev_t = t

            if iface and now - last_vnstat >= float(server["collector"]["vnstat_interval_seconds"]):
                last_vnstat = now
                v = sysmetrics.vnstat_month_tx_bytes(iface)
                if v is not None:
                    month_tx = v

            # 라이브별
            lives, _ = config.load_all_lives(server=server, cross=False)
            running = 0
            for lv in lives:
                st = read_json(paths.status_file(lv.id)) or {}
                state = st.get("state", "unknown")
                fpid = st.get("ffmpeg_pid") if state == "running" else None
                if state == "running" and not sysmetrics.pid_alive(fpid):
                    state = "stopped"
                if state == "running":
                    running += 1
                apid = None
                try:
                    apid = int(paths.audio_pid_file(lv.id).read_text().strip())
                except (OSError, ValueError):
                    pass

                def _cpu(pid):
                    if not sysmetrics.pid_alive(pid):
                        return None
                    ticks = sysmetrics.proc_cpu_ticks(pid)
                    if ticks is None:
                        return None
                    before = prev_proc.get(pid)
                    prev_proc[pid] = ticks
                    return None if before is None else 100.0 * (ticks - before) / sysmetrics.CLK_TCK / dt

                conn.execute(
                    "INSERT INTO metrics_live(ts, live_id, state, speed, cpu_pct, rss_mb, audio_cpu_pct, audio_rss_mb, out_time_s)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (now, lv.id, state, st.get("speed"), _cpu(fpid), sysmetrics.proc_rss_mb(fpid) if fpid else None,
                     _cpu(apid), sysmetrics.proc_rss_mb(apid) if sysmetrics.pid_alive(apid) else None, st.get("out_time_s")),
                )
            # 죽은 PID 정리
            for pid in [p for p in prev_proc if not sysmetrics.pid_alive(p)]:
                prev_proc.pop(pid, None)

            conn.execute(
                "INSERT INTO metrics_system(ts, cpu_pct, steal_pct, iowait_pct, mem_used_mb, mem_total_mb, disk_used_gb,"
                " disk_total_gb, net_tx_mbps, net_rx_mbps, month_tx_gb, lives_running) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, cpu, steal, iowait, mem_used, mem_total, d_used, d_total, tx_mbps, rx_mbps,
                 (month_tx / 1e9) if month_tx is not None else None, running),
            )
            write_json_atomic(paths.run_dir / "system.json", {
                "ts": now, "cpu_pct": cpu, "steal_pct": steal, "iowait_pct": iowait, "mem_used_mb": mem_used,
                "mem_total_mb": mem_total, "disk_used_gb": d_used, "disk_total_gb": d_total, "disk_pct": d_pct,
                "net_tx_mbps": tx_mbps, "net_rx_mbps": rx_mbps, "month_tx_bytes": month_tx, "iface": iface,
                "lives_running": running,
            })
            log.debug("cpu=%.1f%% steal=%.2f%% mem=%.0f/%.0fMB disk=%.1f%% tx=%s Mbps lives=%d",
                      cpu, steal, mem_used, mem_total, d_pct, f"{tx_mbps:.1f}" if tx_mbps is not None else "-", running)

            # 디스크 위생 (DESIGN 6.4): 80% 경고, 90% 긴급 정리
            cl = server["cleanup"]
            if d_pct >= float(cl["disk_emergency_percent"]) and now - last_emergency > 3600:
                last_emergency = now
                log.error("디스크 %.1f%% — 긴급 정리 실행", d_pct)
                db.add_event(conn, "error", "collector", f"디스크 {d_pct:.1f}% — 긴급 정리 실행")
                systemctl.action("start", "livemax-cleanup-emergency.service")
            elif d_pct >= float(cl["disk_warn_percent"]) and int(now) % 3600 < interval:
                db.add_event(conn, "warn", "collector", f"디스크 사용률 {d_pct:.1f}%")
        except Exception:  # noqa: BLE001
            log.exception("수집 실패")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
