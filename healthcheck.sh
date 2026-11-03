#!/usr/bin/env bash
# 송출 준비 상태 점검 (DESIGN 10.4).  sudo ./healthcheck.sh
# 아무것도 바꾸지 않는다. 문제 항목은 [FAIL], 주의 항목은 [WARN] 으로 표시하고 FAIL 이 있으면 종료코드 1.
set -uo pipefail
LIVEMAX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$LIVEMAX_HOME/venv/bin/python"
export LIVEMAX_HOME PYTHONPATH="$LIVEMAX_HOME/src"
FAIL=0
ok()   { echo "  [ OK ] $*"; }
warn() { echo "  [WARN] $*"; }
fail() { echo "  [FAIL] $*"; FAIL=1; }
run_lm() { if [[ $EUID -eq 0 ]] && id livemax &>/dev/null; then sudo -u livemax env LIVEMAX_HOME="$LIVEMAX_HOME" PYTHONPATH="$LIVEMAX_HOME/src" "$PY" "$@"; else "$PY" "$@"; fi; }

echo "== 바이너리"
command -v ffmpeg >/dev/null && ok "ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')" || fail "ffmpeg 없음"
command -v ffprobe >/dev/null && ok "ffprobe" || fail "ffprobe 없음"
command -v liquidsoap >/dev/null && ok "liquidsoap $(liquidsoap --version 2>/dev/null | head -1 | awk '{print $2}')" || fail "liquidsoap 없음"
command -v vnstat >/dev/null && ok "vnstat" || warn "vnstat 없음 — 월 트래픽 집계 불가"
[[ -x "$PY" ]] && ok "venv" || { fail "venv 없음 — install.sh 실행 필요"; exit 1; }

echo "== 재인코딩 금지 가드"
run_lm -m livemax.cli check-reencode >/dev/null && ok "송출 명령 = -c:v copy -c:a copy, 필터 없음" || fail "송출 명령 가드 실패"

echo "== 설정"
if out=$(run_lm -m livemax.cli validate 2>&1); then ok "모든 설정 유효"; else fail "설정 오류"; fi
echo "$out" | sed 's/^/         /'
eval "$(run_lm -m livemax.cli paths)"

echo "== 비밀정보"
SEC="$LIVEMAX_HOME/secrets"
[[ "$(stat -c %a "$SEC" 2>/dev/null)" == "700" ]] && ok "secrets/ 권한 700" || warn "secrets/ 권한이 700 이 아님"
[[ -s "$SEC/admin.hash" ]] && ok "관리자 비밀번호 설정됨" || fail "admin.hash 없음 — reset_password.py"
[[ -e "$SEC/admin.must_change" ]] && warn "초기 비밀번호 미변경 — 첫 로그인 후 변경하세요"
for id in $(run_lm -m livemax.cli lives --enabled 2>/dev/null); do
  k="$SEC/$id.key"
  if [[ ! -s "$k" ]]; then fail "$id: 스트림 키 없음 ($k)"
  elif [[ "$(stat -c %a "$k")" != "600" ]]; then warn "$id: 키 권한 $(stat -c %a "$k") — 600 이어야 함"
  else ok "$id: 스트림 키 (권한 600)"; fi
done
if command -v git >/dev/null && git -C "$LIVEMAX_HOME" rev-parse >/dev/null 2>&1; then
  if git -C "$LIVEMAX_HOME" ls-files --error-unmatch secrets config media >/dev/null 2>&1; then fail "secrets/ config/ media/ 중 git 에 추적되는 파일이 있음!"; else ok "secrets/ config/ 는 git 추적 대상 아님"; fi
fi

echo "== 디스크"
for d in "$LM_MEDIA_ROOT" "$LM_STATE_DIR" "$LM_LOG_DIR"; do
  [[ -d "$d" ]] || { fail "$d 없음"; continue; }
  pct=$(df --output=pcent "$d" | tail -1 | tr -dc '0-9')
  if (( pct >= 90 )); then fail "$d 디스크 ${pct}%"; elif (( pct >= 80 )); then warn "$d 디스크 ${pct}%"; else ok "$d 디스크 ${pct}%"; fi
done
[[ "$(stat -c %d "$LM_MEDIA_ROOT/_incoming" 2>/dev/null)" == "$(stat -c %d "$LM_MEDIA_ROOT/video" 2>/dev/null)" ]] \
  && ok "_incoming ↔ video 같은 파일시스템 (원자적 이동)" || fail "_incoming 과 video 가 다른 파일시스템"

echo "== 서비스"
if command -v systemctl >/dev/null; then
  for u in livemax-ingest livemax-collector livemax-watchdog livemax-scheduler livemax-dashboard; do
    s=$(systemctl is-active "$u.service" 2>/dev/null); [[ "$s" == "active" ]] && ok "$u" || fail "$u: $s"
  done
  systemctl is-active --quiet livemax-cleanup.timer && ok "livemax-cleanup.timer" || warn "cleanup 타이머 비활성"
  for id in $(run_lm -m livemax.cli lives --enabled 2>/dev/null); do
    a=$(systemctl is-active "live-audio@$id.service" 2>/dev/null); v=$(systemctl is-active "live@$id.service" 2>/dev/null)
    [[ "$a" == "active" && "$v" == "active" ]] && ok "$id: audio=$a video=$v" || warn "$id: audio=$a video=$v"
    st="$LM_RUN_DIR/$id.status.json"
    if [[ -f "$st" ]]; then
      run_lm - "$st" <<'EOF'
import json, sys, time
d = json.load(open(sys.argv[1]))
age = time.time() - d.get("updated_at", 0)
print(f"         state={d.get('state')} speed={d.get('speed')} 갱신 {age:.0f}초 전 clip={(d.get('current_clip') or {}).get('path','-').rsplit('/',1)[-1]}")
EOF
    fi
  done
fi

echo "== 업데이트 정책"
if [[ -f /var/run/reboot-required ]]; then
  warn "재부팅 필요 ($(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)) — 점검 시간에 sudo reboot"
else ok "재부팅 대기 중인 업데이트 없음"; fi
ar=$(apt-config dump 2>/dev/null | sed -n 's/^Unattended-Upgrade::Automatic-Reboot "\(.*\)";/\1/p')
[[ "$ar" == "false" ]] && ok "자동 재부팅 꺼짐" || warn "자동 재부팅 설정: ${ar:-기본값} — bootstrap.sh 실행 권장"
apt-config dump 2>/dev/null | grep -q 'Package-Blacklist:: "liquidsoap"' && ok "ffmpeg·Liquidsoap·커널 자동 업데이트 제외" || warn "송출 도구 자동 업데이트 제외 미설정 — bootstrap.sh 실행 권장"
[[ -f /etc/needrestart/conf.d/50-livemax.conf ]] && ok "업데이트 후 서비스 자동 재시작 꺼짐 (needrestart 'l')" || warn "needrestart 자동 재시작 정책 미설정"

echo "== 네트워크 · 보안"
command -v ufw >/dev/null && { ufw status | grep -q "Status: active" && ok "ufw 활성" || warn "ufw 비활성"; }
systemctl is-active --quiet fail2ban && ok "fail2ban" || warn "fail2ban 비활성"
[[ -f /etc/ssh/sshd_config.d/10-livemax.conf ]] && ok "SSH 키 인증 전용" || warn "SSH 비밀번호 로그인이 켜져 있을 수 있음"
while read -r port id; do
  [[ -z "${port:-}" ]] && continue
  addr=$(ss -ltnH "sport = :$port" 2>/dev/null | awk '{print $4}' | head -1)
  if [[ -z "$addr" ]]; then warn "$id harbor :$port 대기 안 함 (오디오 미기동?)"
  elif [[ "$addr" == 127.0.0.1:* ]]; then ok "$id harbor $addr (로컬 전용)"
  else fail "$id harbor 가 $addr 에 노출됨 — 127.0.0.1 이어야 함"; fi
done < <(run_lm -m livemax.cli ports 2>/dev/null)

echo
(( FAIL == 0 )) && echo "결과: 송출 준비 OK" || echo "결과: FAIL 항목을 해결하세요"
exit $FAIL
