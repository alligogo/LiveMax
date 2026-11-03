#!/usr/bin/env bash
# v1 서버(systemd 방식)를 v2(Docker)로 옮긴다. live1 처럼 이미 v1 이 설치된 서버에서만 쓴다.
#
#   curl -fsSL https://raw.githubusercontent.com/alligogo/LiveMax/main/host/migrate-v1.sh | bash
#
# 1. v1 라이브·서비스 정지 및 자동 시작 해제 (송출이 잠시 멈춘다)
# 2. v1 코드를 /opt/livemax-v1 로 옮겨 둠 (설정·키 보관용, 나중에 지워도 됨)
# 3. v1 미디어(/media/video, /media/audio)를 v2 업로드 대기열로 복사 → v2 가 다시 검증·썸네일 생성
#    원본 /media 는 그대로 둔다 (확인 후 직접 지우면 됨)
# 4. get.sh 로 v2 설치
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "root 로 실행하세요"; exit 1; }
RAW="${LIVEMAX_RAW:-https://raw.githubusercontent.com/alligogo/LiveMax/main}"
NEW=/opt/livemax/data

echo "==> 1/4 v1 정지"
for u in $(systemctl list-units --all --plain --no-legend 'live@*' 'live-audio@*' 2>/dev/null | awk '{print $1}'); do
  systemctl disable --now "$u" 2>/dev/null || true
done
for u in livemax-ingest livemax-collector livemax-watchdog livemax-scheduler livemax-dashboard livemax-cleanup.timer caddy; do
  systemctl disable --now "$u" 2>/dev/null || true
done

echo "==> 2/4 v1 코드 보관"
if [[ -d /opt/livemax/.git && ! -d "$NEW" ]]; then
  mv /opt/livemax /opt/livemax-v1
  echo "  /opt/livemax → /opt/livemax-v1"
fi

echo "==> 3/4 미디어 복사 (v2 가 다시 검증)"
for kind in video audio; do
  [[ -d "/media/$kind" ]] || continue
  for d in /media/"$kind"/*/; do
    [[ -d "$d" ]] || continue
    name="$(basename "$d")"
    mkdir -p "$NEW/media/$kind/$name" "$NEW/media/_incoming/$kind/$name"
    cp -n "$d"* "$NEW/media/_incoming/$kind/$name/" 2>/dev/null || true
    echo "  $kind/$name: $(find "$d" -maxdepth 1 -type f | wc -l)개"
  done
done

echo "==> 4/4 v2 설치"
curl -fsSL "$RAW/get.sh" | bash

echo
echo "옮기기 완료. 대시보드에서:"
echo "  1) 영상·음원 탭에서 파일이 검증을 통과했는지 확인 (1~2분)"
echo "  2) 라이브 추가 → 이름·스트림 키·폴더 체크 → 저장하고 시작"
echo "v1 보관본: /opt/livemax-v1 (설정·키), /media (원본). 확인이 끝나면 지워도 됩니다."
