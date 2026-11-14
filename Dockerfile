# LiveMax — 단일 컨테이너 (웹 UI + 라이브 관리 + HTTPS)
#   ffmpeg 6.1 / Liquidsoap 2.2.4 / stunnel 5.72 : Ubuntu 24.04 패키지 (v1 에서 실서버 검증한 조합)
#   Caddy 2 (최신)                              : Let's Encrypt IP 인증서(shortlived 프로필) 지원
FROM caddy:2 AS caddy

FROM ubuntu:24.04
ARG LIVEMAX_VERSION=dev
ENV DEBIAN_FRONTEND=noninteractive TZ=UTC LANG=C.UTF-8 PYTHONUNBUFFERED=1 \
    LIVEMAX_DATA=/data LIVEMAX_VERSION=${LIVEMAX_VERSION} PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg liquidsoap stunnel4 vnstat tini ca-certificates python3 python3-venv \
      mtr-tiny iputils-tracepath \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's|^;\?DatabaseDir .*|DatabaseDir "/data/state/vnstat"|' /etc/vnstat.conf

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY assets /app/assets
COPY src /app/src
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
ENV PYTHONPATH=/app/src LIVEMAX_ASSETS=/app/assets
RUN chmod +x /app/docker/entrypoint.sh && python3 -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('/app/src', quiet=1) else 1)"

VOLUME ["/data"]
EXPOSE 80 443
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
