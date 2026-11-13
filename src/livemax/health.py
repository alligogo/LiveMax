"""건강검진 (v2.19, DESIGN 11-G) — 매일 아침 지난 24시간을 '시청자 경험' 기준으로 판정해 10일 보관.

판정 기준은 우리 서버 숫자가 아니라 **시청자가 끊김·무음을 겪었을 가능성**:
  ✗ 시청자 영향  — 밀림 crit_s(기본 8초) 이상 · 유튜브 재접속 공백 3초 이상 · 소리 없음 · 입력 없음 10초 이상 · 송출 중단
  △ 영향 가능    — 밀림 warn_s(기본 5초) 이상 · 재접속 공백 1~3초 · 중계기 멈춤 3초 이상
  ✓ 내부 처리    — 그보다 짧은 것 (기록만, 최적화 참고)
  · 우리가 한 일 — 업데이트·재부팅 직후 5분, 일일 재시작 직후 90초 (판정에서 뺌)
기준 초는 유튜브 '나쁨' 기록과 맞춰 가며 조정 (11-11 19:27 LIVE 5: 9.4초 밀림 → 유튜브 '나쁨').

원인 칸 — 고칠 수 있는 것과 없는 것을 가른다:
  우리 프로그램 (소리 없음·입력 없음·중계기 멈춤·서버 처리 지연) → 몇 초든 고칠 대상
  회선 (여러 유튜브 IP 의 라이브가 같은 순간) · 유튜브 IP (같은 IP 의 라이브만) → 기록, IP 분산으로 피해 줄이기
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger("livemax.health")
KST = timezone(timedelta(hours=9))
KEEP_DAYS = 10
DEFAULTS = {"time_kst": "07:00", "notify_mode": "daily", "warn_s": 5, "crit_s": 8}
OURS_BOOT_S, OURS_RESTART_S, CLUSTER_S = 300, 90, 90


def cfg(settings: dict) -> dict:
    return {**DEFAULTS, **(settings.get("health") or {})}


def _sev(r, warn_s: float, crit_s: float) -> str:
    k, sec, mx = r["kind"], float(r["sec"] or 0), float(r["max_delay"] or 0)
    if k == "slow":
        return "bad" if mx >= crit_s else "warn" if mx >= warn_s else "minor"
    if k == "reconnect":
        return "bad" if sec >= 3 else "warn" if sec >= 1 else "minor"
    if k in ("no_audio", "down"):
        return "bad"
    if k == "ffmpeg_exit":  # 예상 밖 종료 = 우리 프로그램 문제 — 짧아도 고칠 대상이라 최소 △ (v2.22)
        return "bad" if sec >= crit_s else "warn"
    if k == "no_input":
        return "bad" if sec >= 10 else "warn"
    if k == "stall":
        return "bad" if sec >= crit_s else "warn" if sec >= 3 else "minor"
    return "minor"


def compute(conn, settings: dict, end: float, version: str = "", names: dict | None = None,
            ingest: dict | None = None, span: float = 86400.0) -> dict:
    hc = cfg(settings)
    warn_s, crit_s = float(hc["warn_s"]), float(hc["crit_s"])
    start = end - span
    names = names or {}
    rows = [dict(r) for r in conn.execute(
        "SELECT ts, live_id, kind, sec, max_delay, peer, where_, note FROM incidents WHERE ts >= ? AND ts < ? ORDER BY ts",
        (start - OURS_BOOT_S, end)).fetchall()]
    boots = [r["ts"] for r in rows if r["kind"] == "boot"]
    planned: dict[str, list[float]] = {}
    for r in rows:
        if r["kind"] == "planned_restart" and r["live_id"]:
            planned.setdefault(r["live_id"], []).append(r["ts"])
    evs = [r for r in rows if r["ts"] >= start and r["kind"] not in ("boot", "planned_restart")]

    def ours(r) -> bool:
        return (any(0 <= r["ts"] - b <= OURS_BOOT_S for b in boots)
                or any(0 <= r["ts"] - t <= OURS_RESTART_S for t in planned.get(r["live_id"] or "", [])))

    for r in evs:
        r["sev"] = "ours" if (r["kind"] == "ip_move" or ours(r)) else _sev(r, warn_s, crit_s)
    net = [r for r in evs if r["kind"] in ("slow", "reconnect") and r["sev"] != "ours"]
    for r in evs:
        k = r["kind"]
        if k in ("no_audio", "no_input", "stall", "ffmpeg_exit") or (k == "slow" and r.get("where_") == "server"):
            r["cause"] = "우리 프로그램"
        elif k == "down":
            r["cause"] = "송출 중단"
        elif k in ("slow", "reconnect"):
            near = [x for x in net if abs(x["ts"] - r["ts"]) <= CLUSTER_S]
            lives = {x["live_id"] for x in near}
            peers = {x["peer"] for x in near if x["peer"]}
            if len(lives) >= 3 and len(peers) >= 2:
                r["cause"] = "회선 (여러 유튜브 IP 동시)"
            else:
                r["cause"] = f"유튜브 IP {r['peer']}" if r["peer"] else "유튜브·경로"
            r["together"] = len(lives)
        else:
            r["cause"] = ""

    counted = [r for r in evs if r["sev"] in ("bad", "warn")]
    bad = [r for r in counted if r["sev"] == "bad"]
    impact = sum(float(r["sec"] or 0) or 8.0 for r in bad)
    # 시청자 기준 가동률: 송출 중이던 라이브·시간 중 시청자 영향 시간을 뺀 비율
    m = [dict(r) for r in conn.execute(
        "SELECT ts, cpu_pct, steal_pct, mem_used_mb, mem_total_mb, disk_used_gb, disk_total_gb, lives_running "
        "FROM metrics_system WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)).fetchall()]
    lives_avg = sum(float(x["lives_running"] or 0) for x in m) / len(m) if m else 0.0
    live_s = lives_avg * span
    avail = max(0.0, 1 - impact / live_s) if live_s > 0 else None

    # 서버 상태 (최적화 참고 — 판정과 따로)
    cpus = [float(x["cpu_pct"] or 0) for x in m]
    cpu5 = [sum(cpus[i:i + 20]) / len(cpus[i:i + 20]) for i in range(0, max(1, len(cpus) - 19))] if cpus else []
    steals = [float(x["steal_pct"] or 0) for x in m]
    nm = [dict(r) for r in conn.execute("SELECT retrans_pct, rtt_avg, per_ip FROM net_metrics WHERE ts >= ? AND ts < ?",
                                        (start, end)).fetchall()]
    rts = [float(x["retrans_pct"]) for x in nm if x["retrans_pct"] is not None]
    rtt = [float(x["rtt_avg"]) for x in nm if x["rtt_avg"] is not None]
    ip_rtt: dict[str, list[float]] = {}
    for x in nm:
        try:
            for ip, (_n, a) in json.loads(x["per_ip"] or "{}").items():
                ip_rtt.setdefault(ip, []).append(float(a))
        except (ValueError, TypeError):
            pass
    last = m[-1] if m else {}
    disk = (100.0 * float(last["disk_used_gb"]) / float(last["disk_total_gb"])) if last.get("disk_total_gb") else None
    server = {
        "cpu_avg": round(sum(cpus) / len(cpus), 1) if cpus else None, "cpu_max": round(max(cpus), 1) if cpus else None,
        "cpu5_max": round(max(cpu5), 1) if cpu5 else None,
        "steal_avg": round(sum(steals) / len(steals), 2) if steals else None, "steal_max": round(max(steals), 1) if steals else None,
        "mem_max_mb": round(max(float(x["mem_used_mb"] or 0) for x in m)) if m else None,
        "mem_total_mb": round(float(last.get("mem_total_mb") or 0)) or None,
        "disk_pct": round(disk, 1) if disk is not None else None,
        "retrans_avg": round(sum(rts) / len(rts), 3) if rts else None, "retrans_max": round(max(rts), 2) if rts else None,
        "rtt_avg": round(sum(rtt) / len(rtt), 1) if rtt else None,
        "ip_rtt": {ip: round(sum(v) / len(v), 1) for ip, v in sorted(ip_rtt.items())},
        "lives_avg": round(lives_avg, 1), "minor": sum(1 for r in evs if r["sev"] == "minor"),
        "ours": sum(1 for r in evs if r["sev"] == "ours"), "ingest": ingest or {},
    }
    warn_server = []
    if server["cpu5_max"] is not None and server["cpu5_max"] >= 70:
        warn_server.append(f"CPU 5분 평균 최고 {server['cpu5_max']}%")
    if server["steal_avg"] is not None and server["steal_avg"] >= 5:
        warn_server.append(f"steal 평균 {server['steal_avg']}%")
    if server["disk_pct"] is not None and server["disk_pct"] >= 85:
        warn_server.append(f"디스크 {server['disk_pct']}%")
    if server["retrans_avg"] is not None and server["retrans_avg"] >= 1:
        warn_server.append(f"재전송률 평균 {server['retrans_avg']}%")
    ing = ingest or {}
    if ing.get("crowded"):  # 고르게 나눈 수(fair)보다 눈에 띄게 몰렸을 때만 (v2.21)
        warn_server.append(f"유튜브 IP 한 곳에 {ing['max']}개 몰림 (고르게 나누면 {ing.get('fair')}개)")

    hourly = {h: [0, 0, 0.0] for h in range(24)}  # KST 시 → [✗, △, 그 시간 평균 라이브 수]
    for r in counted:
        h = datetime.fromtimestamp(r["ts"], KST).hour
        hourly[h][0 if r["sev"] == "bad" else 1] += 1
    hl: dict[int, list[float]] = {}
    for x in m:
        hl.setdefault(datetime.fromtimestamp(x["ts"], KST).hour, []).append(float(x["lives_running"] or 0))
    for h, v in hl.items():
        hourly[h][2] = round(sum(v) / len(v), 1)
    causes: dict[str, int] = {}
    for r in counted:
        causes[r["cause"]] = causes.get(r["cause"], 0) + 1

    def item(r):
        return {"ts": r["ts"], "live": r["live_id"], "name": names.get(r["live_id"] or "", r["live_id"] or ""),
                "kind": r["kind"], "sev": r["sev"], "sec": r["sec"], "max_delay": r["max_delay"], "peer": r["peer"],
                "cause": r.get("cause", ""), "together": r.get("together")}

    return {
        "day": datetime.fromtimestamp(end - 1, KST).strftime("%Y-%m-%d"), "from": start, "to": end, "version": version,
        "verdict": "bad" if bad else "warn" if counted else "ok",
        "avail": avail, "impact_s": round(impact, 1),
        "counts": {"bad": len(bad), "warn": len(counted) - len(bad), "minor": server["minor"], "ours": server["ours"],
                   "no_audio": sum(1 for r in counted if r["kind"] == "no_audio"),
                   "reconnect": sum(1 for r in counted if r["kind"] == "reconnect"),
                   "slow": sum(1 for r in counted if r["kind"] == "slow"),
                   "ours_program": sum(1 for r in counted if r.get("cause") == "우리 프로그램")},
        "causes": causes, "events": [item(r) for r in counted][:200], "server": server, "warn_server": warn_server,
        "hourly": hourly, "thresholds": {"warn_s": warn_s, "crit_s": crit_s},
    }


def store(conn, rep: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO health_days(day, ts, data) VALUES (?,?,?)",
                 (rep["day"], rep["to"], json.dumps(rep, ensure_ascii=False)))
    keep = [r[0] for r in conn.execute("SELECT day FROM health_days ORDER BY day DESC LIMIT ?", (KEEP_DAYS,)).fetchall()]
    if keep:
        conn.execute(f"DELETE FROM health_days WHERE day NOT IN ({','.join('?' * len(keep))})", keep)
    cut = time.time() - (KEEP_DAYS + 1) * 86400  # 원본 사건·경로 측정도 11일만
    conn.execute("DELETE FROM incidents WHERE ts < ?", (cut,))
    conn.execute("DELETE FROM net_metrics WHERE ts < ?", (cut,))


def days(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT data FROM health_days ORDER BY day DESC LIMIT ?", (KEEP_DAYS,)).fetchall():
        try:
            out.append(json.loads(r[0]))
        except ValueError:
            pass
    return list(reversed(out))


MARK = {"ok": "✓ 정상", "warn": "△ 주의", "bad": "✗ 시청자 영향"}


def avail_text(rep: dict) -> str:
    """영향이 없으면 100%, 있으면 99.9986% 처럼 필요한 자리까지만 (v2.20)."""
    a = rep.get("avail")
    if a is None:
        return "-"
    if not rep.get("impact_s") or a >= 0.99999995:
        return "100%"
    return f"{a * 100:.4f}".rstrip("0").rstrip(".") + "%"


def message(rep: dict) -> tuple[str, str]:
    c = rep["counts"]
    av = avail_text(rep)
    s = rep["server"]
    title = f"검진 {MARK[rep['verdict']]}"
    lines = [f"{rep['day'][5:]} · 시청자 기준 가동률 {av} · ✗ {c['bad']}건 △ {c['warn']}건",
             f"서버 {'⚠ ' + ', '.join(rep['warn_server']) if rep['warn_server'] else '✓'} · CPU 평균 {s.get('cpu_avg')}% (최고 {s.get('cpu_max')}%)"]
    if rep["causes"]:
        lines.append("원인: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["causes"].items(), key=lambda x: -x[1])[:3]))
    # ✗ 사건: 같은 분·같은 원인은 한 줄로 묶고(동시 밀림), 우리가 고칠 수 있는 것(우리 프로그램·송출 중단)을 먼저
    groups: dict[tuple, list] = {}
    for e in [e for e in rep["events"] if e["sev"] == "bad"]:
        groups.setdefault((datetime.fromtimestamp(e["ts"], KST).strftime("%H:%M"), e["kind"], e.get("cause", "")), []).append(e)
    order = sorted(groups.items(), key=lambda kv: (kv[0][2] not in ("우리 프로그램", "송출 중단"), kv[0][0]))
    for (t, kind, cause), es in order[:4]:
        e = max(es, key=lambda x: float(x["max_delay"] or x["sec"] or 0))
        what = {"slow": f"밀림 최대 {e['max_delay']}초", "reconnect": f"유튜브 재접속 {e['sec']}초", "no_audio": "소리 없음",
                "no_input": f"입력 없음 {e['sec']}초", "stall": f"중계기 멈춤 {e['sec']}초", "down": "송출 중단",
                "ffmpeg_exit": f"송출 프로그램 종료 {e['sec']}초"}.get(kind, kind)
        who = e["name"] + (f" 외 {len(es) - 1}개" if len(es) > 1 else "")
        lines.append(f"· {t} {who} {what}" + (f" — {cause}" if cause else ""))
    if len(order) > 4:
        lines.append(f"· 그 밖에 {len(order) - 4}건 (검진 화면)")
    return title, "\n".join(lines)


def run(app, stop: threading.Event) -> None:
    """매일 설정 시각(한국)에 지난 24시간 검진 → 저장 → (설정에 따라) 휴대폰 알림. 서버가 그 시각에 꺼져 있었으면 켜진 뒤 바로."""
    while not stop.wait(30):
        try:
            hc = cfg(app.settings)
            now = datetime.now(KST)
            hh, mm = (int(x) for x in str(hc["time_kst"]).split(":"))
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now < due:
                continue
            day = now.strftime("%Y-%m-%d")
            with db.session(app.paths.db) as c:
                if c.execute("SELECT 1 FROM health_days WHERE day = ?", (day,)).fetchone():
                    continue
                rep = compute(c, app.settings, time.time(), app.version, names=app.supervisor.short_names(),
                              ingest=app.supervisor.yt_pool.summary())
                rep["day"] = day
                store(c, rep)
            log.info("건강검진 %s: %s", day, MARK[rep["verdict"]])
            if hc["notify_mode"] == "daily" or rep["verdict"] != "ok":
                title, body = message(rep)
                app.notifier.send("health", title, body, dedupe_s=0)
        except Exception:  # noqa: BLE001 — 검진 실패가 송출에 영향 없게
            log.exception("건강검진 실패")
