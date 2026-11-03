#!/usr/bin/env bash
# LiveMax 설치 (DESIGN 10.1)
#
#   git clone <repo> /opt/livemax && cd /opt/livemax && sudo ./install.sh
#   업데이트:  git pull && sudo ./install.sh
#
# 멱등성: 두 번 실행해도 안전하다. 기존 config/, secrets/, 미디어, DB 는 덮어쓰지 않는다.
# 실행 중인 라이브(live@*, live-audio@*)는 재시작하지 않는다 — 새 코드는 각 라이브의 일일 재시작 때 반영된다.
#
# 옵션:
#   --skip-ssh-hardening   SSH 비밀번호 로그인 비활성화를 건너뜀
#   --skip-firewall        ufw 설정을 건너뜀
#   --no-start             서비스를 시작하지 않음
set -euo pipefail

LIVEMAX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIP_SSH=0; SKIP_FW=0; NO_START=0
for a in "$@"; do
  case "$a" in
    --skip-ssh-hardening) SKIP_SSH=1 ;;
    --skip-firewall) SKIP_FW=1 ;;
    --no-start) NO_START=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $a"; exit 2 ;;
  esac
done

RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; RST=$'\e[0m'
step() { echo; echo "${GRN}==>${RST} $*"; }
warn() { echo "${YEL}[경고]${RST} $*"; }
die()  { echo "${RED}[중단]${RST} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "root 권한이 필요합니다: sudo ./install.sh"
case "$LIVEMAX_HOME" in /home/*|/root/*) die "코드는 /opt/livemax 같은 시스템 경로에 두세요 (서비스가 ProtectHome=yes 로 /home 을 볼 수 없음)";; esac

# ---------------------------------------------------------------------------
step "1/9 사전 점검 (DESIGN 10.4)"
# ---------------------------------------------------------------------------
. /etc/os-release || die "/etc/os-release 없음"
[[ "${ID:-}" == "ubuntu" ]] || die "Ubuntu 전용입니다 (현재: ${PRETTY_NAME:-unknown})"
OS_MAJOR="${VERSION_ID%%.*}"
(( OS_MAJOR >= 24 )) || die "Ubuntu 24.04 이상이 필요합니다 (apt 의 Liquidsoap 2.2.x). 현재: $VERSION_ID"
echo "OS: $PRETTY_NAME"

ROOT_FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
(( ROOT_FREE_GB >= 10 )) || die "디스크 여유 ${ROOT_FREE_GB}GB — 최소 10GB 필요"
echo "디스크 여유: ${ROOT_FREE_GB}GB"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
(( MEM_MB >= 1800 )) || die "RAM ${MEM_MB}MB — 최소 2GB 필요"
(( MEM_MB >= 7000 )) || warn "RAM ${MEM_MB}MB — 라이브 10개 기준 8GB 권장"
echo "RAM: ${MEM_MB}MB"

port_owner() { ss -ltnpH "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -1; }
for p in 443; do
  o="$(port_owner $p)"
  [[ -z "$o" || "$o" == "caddy" ]] || die "포트 $p 를 '$o' 가 사용 중입니다"
done

# ---------------------------------------------------------------------------
step "2/9 패키지 설치"
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg liquidsoap python3-venv python3-pip vnstat ufw fail2ban \
  unattended-upgrades sqlite3 caddy curl ca-certificates logrotate >/dev/null

FF_VER=$(ffmpeg -version | head -1 | awk '{print $3}')
LIQ_VER=$(liquidsoap --version 2>/dev/null | head -1 | awk '{print $2}')
echo "ffmpeg $FF_VER / liquidsoap $LIQ_VER"
ver_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }
ver_ge "${FF_VER%%-*}" "4.4" || die "ffmpeg 4.4 이상 필요 (-stats_period)"
ver_ge "${LIQ_VER%%-*}" "2.2.0" || die "Liquidsoap 2.2 이상 필요 (생성 스크립트가 2.2 문법)"

# ---------------------------------------------------------------------------
step "3/9 서비스 계정 · Python 환경"
# ---------------------------------------------------------------------------
id livemax &>/dev/null || useradd --system --home-dir /var/lib/livemax --shell /usr/sbin/nologin livemax
if [[ ! -x "$LIVEMAX_HOME/venv/bin/python" ]]; then
  python3 -m venv "$LIVEMAX_HOME/venv"
fi
"$LIVEMAX_HOME/venv/bin/pip" install -q --upgrade pip
"$LIVEMAX_HOME/venv/bin/pip" install -q -r "$LIVEMAX_HOME/requirements.txt"

# ---------------------------------------------------------------------------
step "4/9 설정 · 비밀정보 디렉터리 (기존 파일은 보존)"
# ---------------------------------------------------------------------------
install -d -m 750 -o livemax -g livemax "$LIVEMAX_HOME/config"
install -d -m 700 -o livemax -g livemax "$LIVEMAX_HOME/secrets"
if [[ ! -f "$LIVEMAX_HOME/config/server.yaml" ]]; then
  install -m 640 -o livemax -g livemax "$LIVEMAX_HOME/config.example/server.yaml" "$LIVEMAX_HOME/config/server.yaml"
  echo "config/server.yaml 생성 (예시 복사)"
else
  echo "config/server.yaml 유지"
fi
# 라이브 예시는 .example 로만 둔다 — 키 없이 송출이 시도되지 않도록
install -m 640 -o livemax -g livemax "$LIVEMAX_HOME/config.example/ch01.yaml" "$LIVEMAX_HOME/config/ch01.yaml.example"
chmod 600 "$LIVEMAX_HOME"/secrets/* 2>/dev/null || true
chown -R livemax:livemax "$LIVEMAX_HOME/config" "$LIVEMAX_HOME/secrets"

PY="$LIVEMAX_HOME/venv/bin/python"
export LIVEMAX_HOME PYTHONPATH="$LIVEMAX_HOME/src"
eval "$("$PY" -m livemax.cli paths)"

# ---------------------------------------------------------------------------
step "5/9 데이터 디렉터리 (코드와 분리, DESIGN 10.2)"
# ---------------------------------------------------------------------------
for d in "$LM_MEDIA_ROOT"/{_incoming/{video,audio,.tmp},_rejected,video,audio} "$LM_LOG_DIR" "$LM_STATE_DIR" "$(dirname "$LM_DB")"; do
  install -d -m 755 -o livemax -g livemax "$d"
done
chown livemax:livemax "$LM_MEDIA_ROOT"
# _incoming 과 방송 폴더는 같은 파일시스템이어야 rename 이 원자적이다 (DESIGN 4.2)
[[ "$(stat -c %d "$LM_MEDIA_ROOT/_incoming")" == "$(stat -c %d "$LM_MEDIA_ROOT/video")" ]] \
  || die "_incoming 과 video 가 다른 파일시스템입니다 — 원자적 이동 불가"
sudo -u livemax env LIVEMAX_HOME="$LIVEMAX_HOME" PYTHONPATH="$LIVEMAX_HOME/src" "$PY" -c \
  "from livemax import db, config; db.init(config.get_paths().db)"

# ---------------------------------------------------------------------------
step "6/9 관리자 비밀번호 (DESIGN 9.2 — 고정 초기값 금지)"
# ---------------------------------------------------------------------------
sudo -u livemax env LIVEMAX_HOME="$LIVEMAX_HOME" "$PY" "$LIVEMAX_HOME/reset_password.py" --if-missing

# ---------------------------------------------------------------------------
step "7/9 systemd 유닛 · tmpfiles · sudoers · logrotate"
# ---------------------------------------------------------------------------
for f in "$LIVEMAX_HOME"/systemd/*.service "$LIVEMAX_HOME"/systemd/*.timer; do
  sed "s|@LIVEMAX_HOME@|$LIVEMAX_HOME|g" "$f" > "/etc/systemd/system/$(basename "$f")"
done
install -m 644 "$LIVEMAX_HOME/deploy/tmpfiles/livemax.conf" /etc/tmpfiles.d/livemax.conf
systemd-tmpfiles --create /etc/tmpfiles.d/livemax.conf
install -m 440 "$LIVEMAX_HOME/deploy/sudoers/livemax" /etc/sudoers.d/livemax.new
if visudo -cf /etc/sudoers.d/livemax.new >/dev/null; then
  mv /etc/sudoers.d/livemax.new /etc/sudoers.d/livemax
else
  rm -f /etc/sudoers.d/livemax.new; die "sudoers 검증 실패"
fi
install -m 644 "$LIVEMAX_HOME/deploy/logrotate/livemax" /etc/logrotate.d/livemax
systemctl daemon-reload

# ---------------------------------------------------------------------------
step "8/9 보안 기본 조치 (DESIGN 9.4)"
# ---------------------------------------------------------------------------
if (( SKIP_FW == 0 )); then
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow OpenSSH >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  echo "ufw: SSH, 443 만 개방"
fi
systemctl enable --now fail2ban >/dev/null 2>&1 || warn "fail2ban 시작 실패"
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
systemctl enable --now vnstat >/dev/null 2>&1 || warn "vnstat 시작 실패"

if (( SKIP_SSH == 0 )); then
  # 키가 하나도 없는데 비밀번호 로그인을 끄면 서버에 못 들어간다 — 반드시 확인 후 적용
  HAS_KEY=0
  for h in /root /home/*; do [[ -s "$h/.ssh/authorized_keys" ]] && HAS_KEY=1; done
  if (( HAS_KEY )); then
    cat > /etc/ssh/sshd_config.d/10-livemax.conf <<'EOF'
# LiveMax install.sh — 키 인증만 허용 (DESIGN 9.4)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    if sshd -t; then systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true; echo "SSH: 비밀번호 로그인 비활성화"
    else rm -f /etc/ssh/sshd_config.d/10-livemax.conf; warn "sshd 설정 검증 실패 — SSH 강화 건너뜀"; fi
  else
    warn "authorized_keys 가 없어 SSH 비밀번호 로그인 비활성화를 건너뜀. 키 등록 후 install.sh 를 다시 실행하세요"
  fi
fi

if [[ -n "$LM_DOMAIN" ]]; then
  [[ -n "$LM_ACME_EMAIL" ]] || warn "dashboard.acme_email 이 비어 있음 — Let's Encrypt 만료 알림을 받을 수 없음"
  sed -e "s|@DOMAIN@|$LM_DOMAIN|g" -e "s|@DASH_PORT@|$LM_DASH_PORT|g" \
      -e "s|@ACME_EMAIL@|${LM_ACME_EMAIL:-admin@$LM_DOMAIN}|g" -e "s|@MAX_UPLOAD_MB@|$LM_MAX_UPLOAD_MB|g" \
      "$LIVEMAX_HOME/deploy/caddy/Caddyfile.template" > /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null && systemctl enable --now caddy >/dev/null && systemctl reload caddy
  echo "HTTPS: https://$LM_DOMAIN (Caddy + Let's Encrypt)"
else
  systemctl disable --now caddy >/dev/null 2>&1 || true
  warn "dashboard.domain 미설정 — HTTPS 공개 없음. SSH 터널로 접속: ssh -L 8080:127.0.0.1:$LM_DASH_PORT <서버>"
fi

# ---------------------------------------------------------------------------
step "9/9 서비스 시작"
# ---------------------------------------------------------------------------
CORE=(livemax-ingest livemax-collector livemax-watchdog livemax-scheduler livemax-dashboard)
if (( NO_START == 0 )); then
  for u in "${CORE[@]}"; do
    systemctl enable "$u.service" >/dev/null 2>&1
    # 공용 서비스만 재시작해 새 코드를 반영한다. 라이브는 건드리지 않는다.
    systemctl restart "$u.service"
  done
  systemctl enable --now livemax-cleanup.timer >/dev/null 2>&1

  if ! sudo -u livemax env LIVEMAX_HOME="$LIVEMAX_HOME" PYTHONPATH="$LIVEMAX_HOME/src" "$PY" -m livemax.cli validate; then
    warn "설정 오류가 있습니다 — 위 FAIL 항목을 고친 뒤 다시 실행하세요"
  fi
  # 활성 라이브 중 아직 안 돌고 있는 것만 시작 (이미 돌고 있는 라이브는 재시작하지 않음)
  for id in $(sudo -u livemax env LIVEMAX_HOME="$LIVEMAX_HOME" PYTHONPATH="$LIVEMAX_HOME/src" "$PY" -m livemax.cli lives --enabled); do
    if [[ ! -s "$LIVEMAX_HOME/secrets/$id.key" ]]; then warn "$id: 스트림 키 없음 (secrets/$id.key) — 시작 안 함"; continue; fi
    systemctl is-active --quiet "live@$id.service" && { echo "$id: 실행 중 (유지)"; continue; }
    systemctl enable --now "live-audio@$id.service" "live@$id.service"
    echo "$id: 시작"
    sleep 31  # 동시 시작 금지 (DESIGN 11-6)
  done
fi

echo
echo "${GRN}설치 완료.${RST} 상태 확인: sudo $LIVEMAX_HOME/healthcheck.sh"
