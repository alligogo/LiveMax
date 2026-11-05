#!/usr/bin/env bash
# LiveMax 송출 밀림 감시 — 새 방식(중계기)·기존 방식(stunnel) 똑같은 기준으로
#
# 유튜브로 가는 연결마다 '커널 송신 대기(Send-Q)'를 1초마다 본다.
# 회선이 막혀 못 나간 데이터는 송출 방식과 상관없이 여기에 쌓이므로 두 방식을 공평하게 비교할 수 있다.
# 문제 있을 때(대기 500KB 이상 ≈ 5Mbps 기준 0.8초 분량)만 기록한다. 서버 부담은 거의 없음.
#
# 사용:
#   bash qwatch.sh            → 뒤에서 10시간 감시 시작 (SSH 를 닫아도 계속)
#   bash qwatch.sh 3          → 3시간
#   bash qwatch.sh report     → 지금까지 결과 요약 (한국 시각)
#   bash qwatch.sh stop       → 감시 중지
set -u
RAW=/root/qwatch.raw
PIDF=/root/qwatch.pid
TH=$((500 * 1024))

case "${1:-}" in
report)
  [ -s "$RAW" ] || { echo "아직 기록 없음 (밀림이 없었거나 감시를 안 켰음)"; exit 0; }
  # 같은 연결에서 2초 안에 이어진 기록을 한 번의 밀림으로 묶는다 (시간순 정렬, 요약은 맨 아래)
  sort -k3,3 -k1,1n "$RAW" | awk '
    function flush() { if (n) { ep[k]++; d = last - t0 + 1; dur[k] += d
        printf "%d\t%s  %-4s %3d초  최대 %4.1fMB\n", t0, t0s, k, d, mx / 1048576 } }
    { e = $1; port = $3
      if (port != cur || e - last > 2) { flush(); cur = port; t0 = e; t0s = $2; mx = 0; k = $4; n = 0 }
      if ($5 > mx) mx = $5; last = e; n++ }
    END { flush(); print "9999999998\t"
          for (x in ep) printf "9999999999\t%-4s 밀림 %d번, 합계 %d초\n", x, ep[x], dur[x] }' | sort -n -k1,1 | cut -f2-
  echo
  echo "(새 = 새 중계기 1~5번 쪽, 기존 = stunnel 6~10번 쪽 · 시각은 한국 기준 · 원본: $RAW)"
  exit 0 ;;
stop)
  [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null && rm -f "$PIDF" && echo "감시 중지" || echo "돌고 있는 감시 없음"
  exit 0 ;;
_run)
  H="${2:-10}"
  END=$(( $(date +%s) + ${QW_SECS:-$((H * 3600))} ))
  while [ "$(date +%s)" -lt "$END" ]; do
    now=$(date +%s)
    ts=$(TZ=Asia/Seoul date -d "@$now" '+%m-%d_%H:%M:%S')
    ss -tnpH state established '( dport = :443 or dport = :1935 )' 2>/dev/null | awk -v e="$now" -v ts="$ts" -v th="$TH" '
      { off = ($1 ~ /^[A-Za-z]/) ? 1 : 0; sq = $(2 + off); lp = $(3 + off); p = $0
        if (sq + 0 < th) next
        if (p ~ /stunnel/) kind = "기존"; else if (p ~ /python/) kind = "새"; else next
        sub(/.*:/, "", lp); print e, ts, lp, kind, sq }' >> "$RAW"
    sleep 1
  done
  rm -f "$PIDF"
  exit 0 ;;
esac

if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "이미 감시 중 (끝내려면: bash $0 stop · 결과: bash $0 report)"; exit 0
fi
command -v ss >/dev/null || { echo "ss 명령이 없습니다 (apt install iproute2)"; exit 1; }
H="${1:-10}"
: > "$RAW"
nohup bash "$0" _run "$H" >/dev/null 2>&1 &
echo $! > "$PIDF"
echo "감시 시작 — ${H}시간 동안 1초마다 (SSH 를 닫아도 계속)"
echo "결과 보기: bash $0 report"
