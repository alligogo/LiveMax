#!/bin/bash
# LiveMax CPU 측정기 — 2초마다 N번(기본 450번 = 15분) 재서 평균·피크·범인·가능한 라이브 수를 정리한다.
# 사용: bash cpu15.sh        (15분)
#       bash cpu15.sh 150    (5분)
N=${1:-450}
O=$(mktemp -d)
CORES=$(nproc)
LIVES=$(pgrep -cx ffmpeg)
echo "측정 시작 $(date +%H:%M:%S) — $((N * 2 / 60))분 동안 그대로 두세요 (코어 ${CORES}개, 송출 중 라이브 ${LIVES}개)"

top -c -b -d 2 -n "$N" -o %CPU -w 250 | awk -v O="$O" '
function grp(c, a) {
  if (c ~ /ffmpeg/) return "송출(ffmpeg)";
  if (a ~ /rtmp_relay/) return "새 중계기";
  if (c ~ /liquidsoap/) return "음악(liquidsoap)";
  if (a ~ /livemax|uvicorn/) return "관리 프로그램";
  if (c ~ /ksoftirqd/) return "네트워크 처리";
  if (c ~ /stunnel/) return "stunnel(기존 방식)";
  if (c ~ /^top$|\/top$/) return "측정 도구(top)";
  if (c ~ /systemd|journald|containerd|dockerd/) return "시스템(systemd·docker)";
  return "기타";
}
/^top -/ { t = $3; next }
/^%Cpu/ {
  if (s >= 2 && pb > 55) print pt "  " pb "%" cur >> (O "/spikes");
  s++; b = 100 - $8 - $10; st = $16; cur = ""; n = 0;
  if (s >= 2) { print b >> (O "/busy"); print st >> (O "/steal") }
  pb = b; pt = t; next
}
/^ *[0-9]+ / {
  if (s < 2) next;                       # 첫 화면은 측정 오류라 버림
  g = grp($12, $12 " " $13 " " $14); sum[g] += $9;
  if ($9 + 0 >= 8 && n < 4) { cur = cur sprintf("  | %s %.0f%%", g, $9); n++ }
}
END { for (g in sum) printf "%s\t%.1f\n", g, sum[g] / (s - 1) > (O "/groups") }
'

cnt=$(wc -l < "$O/busy")
read -r avg p50 p95 mx <<<"$(sort -n "$O/busy" | awk '{a[NR]=$1; t+=$1} END{printf "%.1f %.1f %.1f %.1f", t/NR, a[int(NR*0.5)], a[int(NR*0.95)], a[NR]}')"
steal=$(awk '{t+=$1} END{printf "%.1f", t/NR}' "$O/steal")
over() { awk -v x="$1" '$1>x{c++} END{print c+0}' "$O/busy"; }

echo
echo "================ 결과 ($(date +%H:%M:%S), ${cnt}번 측정) ================"
echo "서버 전체 CPU   평균 ${avg}%   중간값 ${p50}%   상위5% ${p95}%   최고 ${mx}%   (steal 평균 ${steal}%)"
echo "50% 넘은 횟수 $(over 50)번 · 60% $(over 60)번 · 70% $(over 70)번 · 80% $(over 80)번   (2초 단위)"
echo
echo "--- 누가 CPU 를 쓰나 (평균, 코어 1개=100% / 서버 전체 기준)"
sort -t$'\t' -k2 -nr "$O/groups" | awk -F'\t' -v C="$CORES" '{printf "  %-24s %6.1f%%  (서버의 %.1f%%)\n", $1, $2, $2 / C}'
echo
echo "--- 가장 높았던 순간 5개"
if [ -s "$O/spikes" ]; then sort -t' ' -k3 -nr "$O/spikes" | sort -k3 -nr | head -5; else echo "  55% 넘은 순간 없음"; fi
echo
if [ "$LIVES" -gt 0 ]; then
  awk -v a="$avg" -v p="$p95" -v m="$mx" -v L="$LIVES" 'BEGIN{
    pa = a / L; pp = p / L; pm = m / L;
    printf "--- 라이브 1개당   평균 %.1f%% · 상위5%% %.1f%% · 최고 %.1f%%\n", pa, pp, pm;
    printf "--- 추정 최대 라이브 수 (피크가 80%%를 안 넘게)\n";
    printf "  상위5%% 기준: 약 %d개   |   최고값 기준(보수적): 약 %d개\n", int(80 / pp), int(80 / pm);
  }'
fi
rm -rf "$O"
