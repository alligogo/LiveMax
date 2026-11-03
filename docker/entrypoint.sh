#!/usr/bin/env bash
# 컨테이너 시작: Caddy(HTTPS) + vnstat(월 트래픽) 를 띄우고 livemaxd 로 넘어간다.
set -uo pipefail
DATA="${LIVEMAX_DATA:-/data}"
mkdir -p "$DATA/state/caddy" "$DATA/state/vnstat" "$DATA/state/host" "$DATA/logs"

# ---- 공인 IP (인증서·주소용). 지정이 없으면 기본 경로의 출발지 주소
PUBLIC_IP="${LIVEMAX_PUBLIC_IP:-}"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(python3 -c 'import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("1.1.1.1",53));print(s.getsockname()[0])' 2>/dev/null || true)"
fi
echo "$PUBLIC_IP" > "$DATA/state/public_ip"

# ---- Caddy: https://<IP> 에 Let's Encrypt IP 인증서 (6일짜리, 자동 갱신). 실패하면 자체 인증서로 자동 대체
CF="$DATA/state/caddy/Caddyfile"
write_caddy() {  # $1 = acme 사용 여부
  local acme_block=""
  if [[ "$1" == yes ]]; then
    acme_block="issuer acme {
      profile shortlived
    }"
  fi
  cat > "$CF" <<EOF
{
  admin off
  storage file_system $DATA/state/caddy
  default_sni ${PUBLIC_IP:-localhost}
  ${LIVEMAX_EMAIL:+email $LIVEMAX_EMAIL}
}
https://${PUBLIC_IP:-localhost} {
  tls {
    $acme_block
    issuer internal
  }
  encode gzip
  request_body {
    max_size 64MB
  }
  reverse_proxy 127.0.0.1:8080 {
    flush_interval -1
  }
}
http:// {
  redir https://${PUBLIC_IP:-localhost}{uri} permanent
}
EOF
}
write_caddy yes
if ! caddy validate --config "$CF" --adapter caddyfile >/dev/null 2>&1; then
  echo "[entrypoint] 이 Caddy 버전은 IP 인증서 프로필을 지원하지 않음 — 자체 인증서로 시작"
  write_caddy no
fi
( while true; do caddy run --config "$CF" --adapter caddyfile >>"$DATA/logs/caddy.log" 2>&1; sleep 3; done ) &

# ---- vnstat: 이 서버의 월 누적 송신량 (FUP 감시). 호스트 네트워크를 그대로 보므로 실제 회선 기준
( vnstatd -n >>"$DATA/logs/vnstat.log" 2>&1 || true ) &

echo "[entrypoint] LiveMax ${LIVEMAX_VERSION:-dev} — https://${PUBLIC_IP}"
exec python3 -m livemax.daemon
