#!/usr/bin/env bash
# LiveMax 송출 통계 — 라이브별 '유튜브로 실제 나간 속도·밀림' (중계기 기록, 15초 간격)
# 사용: bash relaystats.sh [최근 몇 시간, 기본 12]
H="${1:-12}"
docker exec -i livemax python3 - "$H" <<'PY'
import sqlite3, sys, time
from datetime import datetime, timedelta, timezone
h = float(sys.argv[1]) if len(sys.argv) > 1 else 12
c = sqlite3.connect(sys.argv[2] if len(sys.argv) > 2 else "/data/state/livemax.db")
kst = timezone(timedelta(hours=9))
rows = c.execute("SELECT ts, live_id, kbps_in, kbps_out, delay_s FROM metrics_live WHERE ts > ? AND kbps_in IS NOT NULL"
                 " ORDER BY live_id, ts", (time.time() - h * 3600,)).fetchall()
print(f"최근 {h:g}시간 · 라이브별 (한국 시각)")
by = {}
for r in rows: by.setdefault(r[1], []).append(r)
for lid, rs in by.items():
    bad = [r for r in rs if (r[4] or 0) >= 1 or (r[2] and r[3] is not None and r[3] < r[2] * 0.7)]
    print(f"  {lid:8s} 평균 보냄 {sum(r[3] or 0 for r in rs)/len(rs):.0f}kbps · 최대 밀림 {max(r[4] or 0 for r in rs):.1f}초 · 이상 {len(bad)}회 / {len(rs)}회")
    for r in bad[-10:]:
        print(f"      {datetime.fromtimestamp(r[0], kst):%m-%d %H:%M:%S}  받음 {r[2]:.0f} → 유튜브 {r[3]:.0f}kbps  밀림 {r[4]:.1f}초")
PY
