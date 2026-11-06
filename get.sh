#!/usr/bin/env bash
# LiveMax 설치 — 새 서버(Ubuntu 22.04/24.04)에서 root 로 한 줄:
#
#   curl -fsSL https://raw.githubusercontent.com/alligogo/LiveMax/main/get.sh | sudo bash
#
# 하는 일 (여러 번 실행해도 안전, 데이터는 /opt/livemax/data 에 보존):
#   1. 패키지 업데이트 + Docker 설치
#   2. 서버 시계 UTC, IPv4 우선 (v1 운영: 유튜브행 IPv6 경로가 10분마다 끊김), 송출용 TCP 설정(BBR + fq)
#   3. 방화벽 22·80·443, fail2ban, 자동 보안 업데이트 (자동 재부팅·Docker·커널 자동 업데이트 제외)
#   4. SSH 키로 접속 중이면 비밀번호 로그인 끄기 (키 없이 끄면 잠기므로 확인될 때만)
#   5. LiveMax 컨테이너 실행 → 접속 주소와 초기 비밀번호 출력
set -euo pipefail
RAW="${LIVEMAX_RAW:-https://raw.githubusercontent.com/alligogo/LiveMax/main}"
DATA=/opt/livemax/data
GRN=$'\e[32m'; YEL=$'\e[33m'; RED=$'\e[31m'; BLD=$'\e[1m'; RST=$'\e[0m'
step() { echo; echo "${GRN}==>${RST} ${BLD}$*${RST}"; }
warn() { echo "${YEL}[주의]${RST} $*"; }
die()  { echo "${RED}[중단]${RST} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "root 로 실행하세요:  curl -fsSL $RAW/get.sh | sudo bash"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]] || die "Ubuntu/Debian 전용입니다 (현재: ${PRETTY_NAME:-?})"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1
APT=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
T0=$(date +%s)

# ---------------------------------------------------------------------------
step "1/5 패키지 업데이트 + Docker"
# ---------------------------------------------------------------------------
apt-get update -q
apt-get "${APT[@]}" upgrade
apt-get "${APT[@]}" install ca-certificates curl git ufw fail2ban python3-systemd unattended-upgrades needrestart docker.io
systemctl enable --now docker >/dev/null

# ---------------------------------------------------------------------------
step "2/5 시계 UTC · IPv4 우선 · 송출 TCP(BBR)"
# ---------------------------------------------------------------------------
timedatectl set-timezone UTC
timedatectl set-ntp true || true
grep -q '^precedence ::ffff:0:0/96  *100' /etc/gai.conf 2>/dev/null || echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf
# 유튜브로 보내는 연결의 속도 조절을 BBR 로: 기본(CUBIC)은 회선에서 패킷 몇 개만 사라져도 10개 라이브가 동시에
# 속도를 확 줄여 서버 송신이 반토막 났다가 수십 초에 걸쳐 회복한다(live1 실측 52→28Mbps). fq 는 고르게 흘려보낸다.
# 이미 붙어 있는 연결은 그대로라 새로 맺는 연결부터 적용된다 (설치 직후 라이브가 처음 켜질 때부터).
if modprobe tcp_bbr 2>/dev/null && grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control; then
  echo tcp_bbr > /etc/modules-load.d/livemax-bbr.conf
  printf 'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n' > /etc/sysctl.d/99-livemax-bbr.conf
  sysctl -q -p /etc/sysctl.d/99-livemax-bbr.conf || warn "BBR 설정 적용 실패 — 기본(CUBIC)으로 계속"
else
  warn "이 서버 커널은 BBR 을 지원하지 않음 — 기본(CUBIC)으로 계속"
fi

# ---------------------------------------------------------------------------
step "3/5 방화벽 · fail2ban · 자동 업데이트 정책"
# ---------------------------------------------------------------------------
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null      # enable 전에 — 지금 접속이 끊기지 않게
ufw allow 80/tcp >/dev/null       # HTTPS 인증서 발급·https 로 넘겨주기
ufw allow 443/tcp >/dev/null      # 대시보드
ufw --force enable >/dev/null
# 막은 접속을 한 줄씩 기록하면 인터넷 스캐너 때문에 로그(journald)가 계속 바쁘다 — 차단은 그대로, 기록만 끔
ufw logging off >/dev/null
# 10분마다 도는 sysstat 수집은 LiveMax 수집기(15초)와 중복이고 CPU 피크를 만든다 (live1 실측) — 깔려 있으면 끔
systemctl disable --now sysstat-collect.timer sysstat-summary.timer >/dev/null 2>&1 || true
cat > /etc/fail2ban/jail.d/livemax.local <<'EOF'
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
systemctl enable fail2ban >/dev/null 2>&1; systemctl restart fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
# 보안 패치는 자동, 방송을 끊는 것은 제외: 자동 재부팅, 커널, Docker 엔진(업데이트되면 모든 라이브가 동시에 재시작됨)
cat > /etc/apt/apt.conf.d/52livemax-unattended-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Package-Blacklist {
    "linux-image"; "linux-generic"; "linux-headers"; "linux-modules";
    "docker.io"; "docker-ce"; "containerd"; "runc";
};
EOF
install -d /etc/needrestart/conf.d
echo "\$nrconf{restart} = 'l';" > /etc/needrestart/conf.d/50-livemax.conf
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
step "4/5 SSH"
# ---------------------------------------------------------------------------
CLIENT_IP="${SSH_CLIENT:-}"; CLIENT_IP="${CLIENT_IP%% *}"
# ubuntu 로 접속 → sudo -i 하면 SSH_CLIENT 가 지워진다 (OVH 등) → 이 셸의 터미널(pts/N)로 접속한 곳을 찾는다
if [[ -z "$CLIENT_IP" ]]; then
  TTY_NOW="$(ps -o tty= -p $$ 2>/dev/null | tr -d ' ')"
  CLIENT_IP="$(who 2>/dev/null | awk -v t="$TTY_NOW" '$2==t {gsub(/[()]/, "", $NF); print $NF; exit}')"
fi
if [[ -f /etc/ssh/sshd_config.d/10-livemax.conf ]]; then
  echo "이미 키 인증 전용"
elif [[ -n "$CLIENT_IP" ]] && journalctl -u ssh -u sshd --since "-12h" 2>/dev/null | grep -q "Accepted publickey for .* from $CLIENT_IP "; then
  # 지금 이 접속이 키로 들어왔음이 확인될 때만 비밀번호 로그인을 끈다 (잠김 방지)
  cat > /etc/ssh/sshd_config.d/10-livemax.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  if sshd -t; then systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true; echo "비밀번호 로그인 끔 (키 인증 전용)"
  else rm -f /etc/ssh/sshd_config.d/10-livemax.conf; warn "sshd 설정 검증 실패 — 변경 취소"; fi
else
  warn "SSH 키로 접속한 기록이 없어 비밀번호 로그인을 유지합니다. (보안을 위해 나중에 SSH 키 등록 후 다시 실행 권장)"
fi

# ---------------------------------------------------------------------------
step "5/5 LiveMax 실행"
# ---------------------------------------------------------------------------
curl -fsSL "$RAW/host/livemax" -o /usr/local/bin/livemax
chmod 755 /usr/local/bin/livemax
cat > /etc/systemd/system/livemax-tick.service <<'EOF'
[Unit]
Description=LiveMax host tick (web update requests, reboot-required flag)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/livemax tick
EOF
cat > /etc/systemd/system/livemax-tick.timer <<'EOF'
[Unit]
Description=LiveMax host tick every 30s
[Timer]
OnBootSec=60
OnUnitActiveSec=30
AccuracySec=5
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now livemax-tick.timer >/dev/null
mkdir -p "$DATA"
/usr/local/bin/livemax update

echo "초기 비밀번호를 기다리는 중..."
for _ in $(seq 1 60); do [[ -s "$DATA/secrets/admin.initial_password" || -s "$DATA/secrets/admin.hash" ]] && break; sleep 2; done
IP="$(cat "$DATA/state/public_ip" 2>/dev/null || hostname -I | awk '{print $1}')"
PW="$(cat "$DATA/secrets/admin.initial_password" 2>/dev/null || true)"
echo
echo "${GRN}==================================================================${RST}"
echo "${GRN}  LiveMax 설치 완료  ($(( $(date +%s) - T0 ))초)${RST}"
echo "${GRN}  접속:   https://$IP${RST}"
if [[ -n "$PW" ]]; then
  echo "${GRN}  아이디: admin${RST}"
  echo "${GRN}  초기 비밀번호: $PW   (첫 로그인 때 바꾸게 됩니다)${RST}"
else
  echo "${GRN}  (기존 비밀번호 유지 — 분실 시: sudo livemax password)${RST}"
fi
echo "${GRN}==================================================================${RST}"
echo "인증서 발급에 1~2분 걸릴 수 있습니다. 그동안 '안전하지 않음' 경고가 보이면 잠시 뒤 새로고침하세요."
[[ -f /var/run/reboot-required ]] && warn "커널 업데이트가 설치됐습니다. 라이브를 띄우기 전에 'reboot' 하면 깔끔합니다 (재부팅 후 자동 시작)."
exit 0
