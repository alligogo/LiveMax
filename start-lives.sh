#!/usr/bin/env bash
# 설정해 둔 라이브 중 "활성(enabled) + 스트림 키 있음 + 아직 안 도는 것"만 시작한다.
#
#   sudo /opt/livemax/start-lives.sh
#
# - 이미 송출 중인 라이브는 절대 건드리지 않는다 (DESIGN 11-8).
# - 여러 개를 시작할 때는 31초 간격을 둔다 (DESIGN 11-6, 동시 시작 금지).
# - 설정에 오류가 있으면 아무것도 시작하지 않는다.
# 대시보드에서 라이브를 추가·활성화한 뒤 이걸 실행하면 된다. (PC 메뉴 "라이브 시작"이 이걸 부른다)
set -euo pipefail
LIVEMAX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$LIVEMAX_HOME/venv/bin/python"
[[ $EUID -eq 0 ]] || { echo "root 권한이 필요합니다: sudo $0"; exit 1; }
[[ -x "$PY" ]] || { echo "설치가 안 되어 있습니다: cd $LIVEMAX_HOME && ./install.sh"; exit 1; }
run_lm() { sudo -u livemax env LIVEMAX_HOME="$LIVEMAX_HOME" PYTHONPATH="$LIVEMAX_HOME/src" "$PY" "$@"; }

if ! run_lm -m livemax.cli validate; then
  echo "[중단] 설정 오류가 있어 라이브를 시작하지 않습니다. 위 FAIL 항목을 대시보드에서 고치세요."
  exit 1
fi

started=0; running=0; skipped=0
for id in $(run_lm -m livemax.cli lives --enabled); do
  if [[ ! -s "$LIVEMAX_HOME/secrets/$id.key" ]]; then
    echo "  $id: 스트림 키 없음 — 대시보드에서 키를 입력하세요 (건너뜀)"; skipped=$((skipped + 1)); continue
  fi
  if systemctl is-active --quiet "live@$id.service"; then
    echo "  $id: 송출 중 (그대로 둠)"; running=$((running + 1)); continue
  fi
  if (( started > 0 )); then
    echo "  … 동시 시작 금지 — 31초 대기"; sleep 31
  fi
  systemctl enable --now "live-audio@$id.service" "live@$id.service"
  echo "  $id: 시작함"
  started=$((started + 1))
done
echo "결과: 새로 시작 ${started}개 / 이미 송출 중 ${running}개 / 건너뜀 ${skipped}개"
if (( started > 0 )); then
  echo "1~2분 뒤 상태 확인: sudo $LIVEMAX_HOME/healthcheck.sh"
fi
