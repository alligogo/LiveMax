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
# 화면 이름: '라이브명 (채널명) · live13' (DESIGN 11-D 10) — 못 읽으면 번호만
srv, names = "", {}
try:
    from livemax import catalog, channels, config
    from livemax.paths import Paths, data_dir
    p = Paths(data_dir()); st = config.load_settings(p)
    chs = {ch.id: ch.name for ch in channels.ChannelStore(p).all()}
    srv = config.server_name(st)
    for lv in config.LiveStore(p, st).all():
        names[lv.id] = f"{lv.name} ({chs[lv.channel_id]})" if chs.get(lv.channel_id) else lv.name
    order = lambda lid: catalog.natural_key(names.get(lid, lid))
except Exception:
    order = lambda lid: lid
print(f"최근 {h:g}시간 · 라이브별 (한국 시각)" + (f" · 서버 {srv}" if srv else ""))
by = {}
for r in rows: by.setdefault(r[1], []).append(r)
for lid in sorted(by, key=order):
    rs = by[lid]
    bad = [r for r in rs if (r[4] or 0) >= 1 or (r[2] and r[3] is not None and r[3] < r[2] * 0.7)]
    print(f"  {names.get(lid, '?')} · {lid}\n      평균 보냄 {sum(r[3] or 0 for r in rs)/len(rs):.0f}kbps · 최대 밀림 {max(r[4] or 0 for r in rs):.1f}초 · 이상 {len(bad)}회 / {len(rs)}회")
    for r in bad[-10:]:
        print(f"      {datetime.fromtimestamp(r[0], kst):%m-%d %H:%M:%S}  받음 {r[2]:.0f} → 유튜브 {r[3]:.0f}kbps  밀림 {r[4]:.1f}초")
PY
