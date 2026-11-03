#!/usr/bin/env bash
# LiveMax 서버 최초 설정 — 갓 생성된 Ubuntu 24.04 서버에서 root 로 한 번 실행한다.
#
#   PC 에서:   scp bootstrap.sh root@<서버IP>:/root/
#   서버에서:  bash /root/bootstrap.sh --pubkey "ssh-ed25519 AAAA... me@pc"
#
# 하는 일 (모두 멱등 — 여러 번 실행해도 안전):
#   1. 패키지 업데이트 (apt update + full-upgrade) + 기본 도구
#   2. 타임존 UTC + NTP 시간 동기화 (DESIGN 4.5: 서버 시계는 항상 UTC)
#   3. 방화벽 ufw — SSH, 443 만 개방 (DESIGN 9.4)
#   4. fail2ban — SSH 무차별 대입 차단
#   5. 자동 업데이트 정책 — 보안 패치는 자동, 자동 재부팅 금지, ffmpeg·Liquidsoap·커널 제외,
#      업데이트 후 서비스 자동 재시작 금지 (needrestart 'l')
#   6. SSH 키 인증 전환 — 키 등록 → (키 접속 확인 후) 비밀번호 로그인 비활성화
#   7. GitHub deploy key 생성 (비공개 저장소 클론용, 읽기 전용)
#
# 옵션:
#   --pubkey "<공개키 한 줄>"   root 의 authorized_keys 에 추가할 PC 공개키
#   --pubkey-file <파일>        위와 같지만 파일에서 읽음
#   --hostname <이름>           호스트명 설정 (예: live1)
#   --yes                       "키로 접속되는지 확인했나요?" 질문을 건너뛰고 비밀번호 로그인을 끈다
#   --keep-password-login       비밀번호 로그인을 끄지 않음 (키 등록만)
#
# 잠김 방지: authorized_keys 가 비어 있으면 비밀번호 로그인을 절대 끄지 않는다.
# 그래도 잠겼다면 Contabo 고객 패널의 VNC 콘솔로 들어가 /etc/ssh/sshd_config.d/10-livemax.conf 를 지운다.
set -euo pipefail

PUBKEY=""; PUBKEY_FILE=""; NEW_HOSTNAME=""; ASSUME_YES=0; KEEP_PW=0
while (( $# )); do
  case "$1" in
    --pubkey) PUBKEY="${2:-}"; shift 2 ;;
    --pubkey-file) PUBKEY_FILE="${2:-}"; shift 2 ;;
    --hostname) NEW_HOSTNAME="${2:-}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --keep-password-login) KEEP_PW=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1 (--help 참고)"; exit 2 ;;
  esac
done

RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; BLD=$'\e[1m'; RST=$'\e[0m'
step() { echo; echo "${GRN}==>${RST} ${BLD}$*${RST}"; }
warn() { echo "${YEL}[경고]${RST} $*"; }
die()  { echo "${RED}[중단]${RST} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "root 로 실행하세요: sudo bash bootstrap.sh"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "Ubuntu 전용 (현재: ${PRETTY_NAME:-?})"
(( ${VERSION_ID%%.*} >= 24 )) || die "Ubuntu 24.04 이상이 필요합니다 (현재 $VERSION_ID). 서버를 24.04 이미지로 다시 설치하세요"
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

# ---------------------------------------------------------------------------
step "1/7 패키지 업데이트"
# ---------------------------------------------------------------------------
apt-get update -q
apt-get "${APT_OPTS[@]}" full-upgrade
apt-get "${APT_OPTS[@]}" install git curl ca-certificates ufw fail2ban python3-systemd unattended-upgrades needrestart htop
apt-get -y -q autoremove

if [[ -n "$NEW_HOSTNAME" ]]; then
  [[ "$NEW_HOSTNAME" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "호스트명 형식 오류: $NEW_HOSTNAME"
  hostnamectl set-hostname "$NEW_HOSTNAME"
  grep -q "127.0.1.1 $NEW_HOSTNAME" /etc/hosts || echo "127.0.1.1 $NEW_HOSTNAME" >> /etc/hosts
  echo "호스트명: $NEW_HOSTNAME"
fi

# ---------------------------------------------------------------------------
step "2/7 타임존 UTC + 시간 동기화"
# ---------------------------------------------------------------------------
# 서버 시계는 항상 UTC. 편성은 라이브별 timezone(지역명)으로만 변환한다 (DESIGN 4.5)
timedatectl set-timezone UTC
timedatectl set-ntp true || warn "NTP 활성화 실패 — timedatectl status 로 확인"
echo "현재 시각: $(date -u '+%Y-%m-%d %H:%M:%S %Z')"

# ---------------------------------------------------------------------------
step "3/7 방화벽 (ufw): SSH, 443 만 개방"
# ---------------------------------------------------------------------------
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null      # 반드시 enable 전에 — 접속 중인 세션이 끊기지 않도록
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status | sed 's/^/  /'

# ---------------------------------------------------------------------------
step "4/7 fail2ban (SSH 무차별 대입 차단)"
# ---------------------------------------------------------------------------
cat > /etc/fail2ban/jail.d/livemax.local <<'EOF'
# LiveMax bootstrap.sh — Ubuntu 24.04 는 journald 기반이라 backend=systemd
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban
sleep 1
fail2ban-client status sshd 2>/dev/null | sed 's/^/  /' || warn "fail2ban sshd jail 상태 확인 실패"

# ---------------------------------------------------------------------------
step "5/7 자동 업데이트 정책 (보안 패치는 받되, 방송을 끊는 요인은 차단)"
# ---------------------------------------------------------------------------
# (a) 보안 업데이트는 매일 계속 받는다
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
// LiveMax bootstrap.sh — 보안 업데이트 자동 설치 유지
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

# (b) 자동 재부팅 금지 + 송출 핵심 도구·커널은 자동 업데이트 제외
#     50unattended-upgrades 는 직접 고치지 않는다: ucf 가 관리하는 배포판 파일이라 수정하면
#     unattended-upgrades 자신의 업데이트가 "설정 파일 충돌"로 자동 업데이트에서 빠진다.
#     apt 는 apt.conf.d 를 이름순으로 읽으므로 52- 파일의 값이 50- 위에 적용된다
#     (Automatic-Reboot 는 덮어쓰고, Package-Blacklist 는 목록에 추가된다).
#     항목은 패키지명 앞부분 정규식 매칭: "linux-image" → linux-image-6.8.0-xx-generic 등 전부.
cat > /etc/apt/apt.conf.d/52livemax-unattended-upgrades <<'EOF'
// LiveMax bootstrap.sh — 24/7 송출 서버 자동 업데이트 정책
// 확인: apt-config dump | grep Unattended-Upgrade::
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Package-Blacklist {
    // 송출 핵심 도구
    "ffmpeg";
    "liquidsoap";
    // ffmpeg·Liquidsoap 이 실제로 쓰는 라이브러리 (ffmpeg 패키지 자체는 얇은 실행파일뿐)
    "libavcodec";
    "libavformat";
    "libavutil";
    "libavfilter";
    "libavdevice";
    "libswresample";
    "libswscale";
    "libpostproc";
    // 커널 (수동 재부팅으로만 적용)
    "linux-image";
    "linux-generic";
    "linux-headers";
    "linux-modules";
};
EOF

# (c) 업데이트 후 서비스 자동 재시작 금지 — 'l' = 재시작이 필요한 서비스를 목록으로만 보여줌
#     needrestart.conf 도 dpkg 설정 파일이라 직접 고치지 않고, 이 파일이 끝에서 읽는 conf.d 를 쓴다.
install -d -m 755 /etc/needrestart/conf.d
cat > /etc/needrestart/conf.d/50-livemax.conf <<'EOF'
# LiveMax bootstrap.sh — 패키지 업데이트 후 서비스를 자동 재시작하지 않음 (목록만 표시)
$nrconf{restart} = 'l';
EOF

systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
echo "자동 재부팅: $(apt-config dump | sed -n 's/^Unattended-Upgrade::Automatic-Reboot "\(.*\)";/\1/p')"
echo "자동 업데이트 제외: $(apt-config dump | sed -n 's/^Unattended-Upgrade::Package-Blacklist:: "\(.*\)";/\1/p' | tr '\n' ' ')"
if command -v needrestart >/dev/null; then
  echo "needrestart 재시작 모드: $(perl -e 'our %nrconf; do "/etc/needrestart/needrestart.conf"; print $nrconf{restart}')"
fi
echo "커널·송출 도구 업데이트는 점검 시간에 수동으로: apt upgrade && reboot (대시보드에 '재부팅 필요' 표시)"

# ---------------------------------------------------------------------------
step "6/7 SSH 키 인증 전환"
# ---------------------------------------------------------------------------
install -d -m 700 /root/.ssh
AK=/root/.ssh/authorized_keys
touch "$AK"; chmod 600 "$AK"
if [[ -n "$PUBKEY_FILE" ]]; then
  [[ -f "$PUBKEY_FILE" ]] || die "공개키 파일 없음: $PUBKEY_FILE"
  PUBKEY="$(head -1 "$PUBKEY_FILE")"
fi
if [[ -n "$PUBKEY" ]]; then
  [[ "$PUBKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com)\ [A-Za-z0-9+/=]+ ]] \
    || die "공개키 형식이 아닙니다 (ssh-ed25519 AAAA... 형태의 .pub 내용). 개인키를 넣은 건 아닌지 확인하세요"
  if grep -qF "$(echo "$PUBKEY" | awk '{print $2}')" "$AK"; then
    echo "공개키 이미 등록됨"
  else
    echo "$PUBKEY" >> "$AK"; echo "공개키 등록: $(echo "$PUBKEY" | awk '{print $1, $3}')"
  fi
fi
KEYS=$(grep -cE '^(ssh-|ecdsa-|sk-)' "$AK" || true)
echo "root 에 등록된 SSH 키: ${KEYS}개"

SSHD_DROPIN=/etc/ssh/sshd_config.d/10-livemax.conf
disable_pw() {
  # sshd 는 같은 항목의 "처음 나온 값"을 쓴다. 10- 로 시작해 클라우드 이미지의 50-cloud-init.conf 보다 먼저 읽힌다.
  cat > "$SSHD_DROPIN" <<'EOF'
# LiveMax — 키 인증만 허용 (DESIGN 9.4)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
  if sshd -t; then
    systemctl reload ssh 2>/dev/null || systemctl restart ssh
    echo "${GRN}비밀번호 로그인 비활성화 완료${RST} — 이제 키로만 접속됩니다"
  else
    rm -f "$SSHD_DROPIN"; warn "sshd 설정 검증 실패 — 변경을 되돌렸습니다"
  fi
}

if [[ -f "$SSHD_DROPIN" ]]; then
  echo "이미 키 인증 전용으로 설정되어 있음"
elif (( KEEP_PW )); then
  warn "--keep-password-login: 비밀번호 로그인을 유지합니다"
elif (( KEYS == 0 )); then
  warn "등록된 키가 없어 비밀번호 로그인을 끄지 않습니다 (잠김 방지). --pubkey 로 키를 넣고 다시 실행하세요"
elif (( ASSUME_YES )); then
  disable_pw
elif [[ -t 0 ]]; then
  echo
  echo "${BLD}지금 이 창은 닫지 말고, PC 에서 '새 터미널'을 열어 키로 접속되는지 먼저 확인하세요:${RST}"
  echo "    ssh root@$(curl -s -4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
  echo "비밀번호를 묻지 않고 바로 들어가지면 성공입니다."
  read -r -p "키로 접속되는 것을 확인했나요? 비밀번호 로그인을 끕니다 [yes/no]: " ans
  if [[ "$ans" == "yes" ]]; then disable_pw; else warn "건너뜀 — 확인 후 'bash bootstrap.sh --yes' 로 다시 실행하세요"; fi
else
  warn "대화형 터미널이 아니라 확인할 수 없어 비밀번호 로그인을 유지합니다. 키 접속 확인 후 --yes 로 다시 실행하세요"
fi

# ---------------------------------------------------------------------------
step "7/7 GitHub deploy key (비공개 저장소 클론용, 읽기 전용)"
# ---------------------------------------------------------------------------
DK=/root/.ssh/livemax_deploy
if [[ ! -f "$DK" ]]; then
  ssh-keygen -q -t ed25519 -f "$DK" -N "" -C "livemax-deploy@$(hostname)"
fi
if ! grep -q "IdentityFile $DK" /root/.ssh/config 2>/dev/null; then
  printf 'Host github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$DK" >> /root/.ssh/config
fi
chmod 600 /root/.ssh/config
ssh-keyscan -t ed25519 github.com 2>/dev/null >> /root/.ssh/known_hosts && sort -u -o /root/.ssh/known_hosts /root/.ssh/known_hosts

echo
if ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com 2>&1 | grep -q 'successfully authenticated'; then
  # PC 도구(LiveMax.bat)가 공용 deploy key 를 미리 넣어 준 경우 — GitHub 등록이 필요 없다
  echo "${GRN}GitHub 연결 OK${RST} — 이미 등록된 deploy key 입니다 (추가 등록 필요 없음)"
else
  echo "${BLD}아래 한 줄을 GitHub → 저장소 Settings → Deploy keys → Add deploy key 에 붙여넣으세요${RST}"
  echo "(Allow write access 는 체크하지 마세요)"
  echo "------------------------------------------------------------------"
  cat "$DK.pub"
  echo "------------------------------------------------------------------"
fi

echo
echo "${GRN}bootstrap 완료.${RST}"
[[ -f /var/run/reboot-required ]] && warn "커널 업데이트가 있어 재부팅이 필요합니다: reboot  (재부팅 후 다음 단계 진행)"
echo "다음 단계: deploy key 등록 → git clone git@github.com:alligogo/LiveMax.git /opt/livemax → cd /opt/livemax && ./install.sh"
