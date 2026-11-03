# LiveMax

유튜브 24/7 음악 라이브 다중 송출 시스템. 서버 한 대(Contabo Cloud VPS 4)에서 서로 다른 영상·음원 조합의 라이브 여러 개를 동시에, 무중단으로 송출한다. 설계는 [DESIGN.md](DESIGN.md).

- **설치:** 새 서버에서 명령 1줄
- **운영:** 브라우저로 `https://서버IP` → 비밀번호. 라이브 시작·정지·설정, 미디어 업로드, 업데이트를 전부 웹에서
- **비용:** 재인코딩을 하지 않아 라이브 1개가 CPU 약 4%, 송신 약 5.9 Mbps (v1 실측) → 서버 1대에 10~15개

새 서버를 여는 순서는 **[docs/NEW_SERVER.md](docs/NEW_SERVER.md)**.

---

## 설치

새 Ubuntu 서버에 root 로 접속해서:

```bash
curl -fsSL https://raw.githubusercontent.com/alligogo/LiveMax/main/get.sh | bash
```

끝나면 접속 주소(`https://서버IP`)와 초기 비밀번호가 나온다. 첫 로그인 때 비밀번호를 바꾼다.

`get.sh`가 하는 일 (여러 번 실행해도 안전, 데이터 보존):

1. 패키지 업데이트, Docker 설치
2. 서버 시계 UTC, **IPv4 우선** (v1 운영에서 유튜브행 IPv6 경로가 10분마다 끊겼다)
3. 방화벽 22·80·443, fail2ban, 자동 보안 업데이트 — 자동 재부팅·커널·Docker 엔진은 자동 업데이트에서 제외 (방송이 끊기지 않게)
4. 지금 SSH 키로 접속 중임이 확인되면 비밀번호 로그인 끄기 (확인 안 되면 그대로 둠 — 잠김 방지)
5. LiveMax 컨테이너 실행, 30초 주기 호스트 타이머(`livemax tick`) 등록

업데이트: 웹 **설정 → 최신 버전으로 업데이트**, 또는 서버에서 `sudo livemax update`. 데이터(`/opt/livemax/data`)는 그대로 남는다.

---

## 구조

```
서버 (Ubuntu)
 ├─ /usr/local/bin/livemax        update · status · logs · password · tick
 ├─ livemax-tick.timer            웹 업데이트 요청 처리, 재부팅 필요 표시 전달
 └─ Docker 컨테이너 livemax (--network host, 재부팅 시 자동 시작)
     ├─ Caddy       https://서버IP — Let's Encrypt IP 인증서(6일, 자동 갱신), 실패 시 자체 인증서
     └─ livemaxd    웹 UI/API (127.0.0.1:8080)
                    라이브마다 독립 프로세스:  Liquidsoap(음원) → ffmpeg(영상+먹싱, copy) → stunnel(TLS)
                    감시 · 일일 재시작(라이브마다 2분 간격) · 업로드 검증 · 지표 · 자동 정리

데이터 /opt/livemax/data  (컨테이너의 /data)
  config/lives/live1.yaml …   라이브 설정 (웹에서 관리)
  secrets/                    스트림 키, 관리자 비밀번호 해시 (700)
  media/_incoming/…           업로드 스테이징 (SFTP 로 올릴 때도 여기)
  media/video|audio/<폴더>/   방송 원본
  state/ (DB·목록·썸네일·인증서), logs/
```

### 송출 경로 — 서버는 재인코딩하지 않는다 (DESIGN 11-1, 11-2)

| 구간 | 하는 일 |
|---|---|
| 업로드 | ffprobe 로 헤더·패킷만 검사 → 통과하면 원자적으로 방송 폴더로 이동, 실패하면 이유와 함께 격리 |
| 음원 (Liquidsoap) | 폴더 감시·중복방지 셔플 → `%audio.copy` (AAC 프레임 그대로). 크로스페이드를 켠 라이브만 오디오 재인코딩 |
| 영상 (ffmpeg) | 24시간 목록 + 음원 → `-c:v copy -c:a copy` → 로컬 RTMP |
| TLS (stunnel) | RTMPS 암호화를 큰 레코드로 → 유튜브 `a.rtmps.youtube.com` (IPv4 고정, 인증서 검증) |

`ffmpeg_cmd.assert_copy_only()`가 실행 직전에 필터·인코더 옵션을 검사하고, 테스트가 소스 전체를 스캔해 필터 문자열을 막는다.

---

## v1 운영에서 배운 것 (코드에 반영됨)

| 문제 | 원인 | v2 |
|---|---|---|
| 송신이 예상보다 48% 많음 (8.4 Mbps) | ffmpeg 내장 RTMPS 가 128바이트마다 TLS 레코드를 만듦 | 라이브별 stunnel 이 큰 레코드로 → **5.9 Mbps** |
| 10분마다 송출 끊김 | 유튜브행 IPv6 경로가 주기적으로 막힘 | stunnel IPv4 고정 + 서버 IPv4 우선 + keepalive |
| 인증서 검증 실패 | RTMPS 호스트를 `a.rtmp.youtube.com`으로 씀 | 정식 `a.rtmps.youtube.com` |
| 대시보드 접속에 도스창·터널 필요 | localhost 바인드만 | `https://서버IP` 정식 인증서 |
| 설치가 복잡 (bootstrap·deploy key·install) | 서버에 직접 설치 | Docker + 명령 1줄, 저장소 공개 |
| 저장해도 송출 중엔 반영 안 됨 | 시작할 때만 설정을 읽음 | **저장하고 지금 적용** (그 라이브만 몇 초) |
| 시간이 서버 기준(UTC)으로 표시 | 서버가 시각 문자열을 만듦 | 브라우저가 현지 시간으로 표시 |
| 시간대 편성·구간·ID·포트 입력이 번거로움 | — | 제거 / 자동 |

---

## 파일

| 경로 | 역할 |
|---|---|
| `get.sh` | 새 서버 설치 (명령 1줄) |
| `host/livemax` | 호스트 명령 — 컨테이너 실행 인자는 여기 한 곳 |
| `Dockerfile`, `docker/entrypoint.sh` | 이미지 (Ubuntu 24.04 + ffmpeg + Liquidsoap + stunnel + Caddy), Caddy 설정 생성 |
| `.github/workflows/image.yml` | 테스트 → 이미지 빌드(ghcr.io) → 컨테이너·HTTPS 스모크 테스트 |
| `src/livemax/daemon.py` | 관리 프로세스 진입점 (웹·감시·업로드·지표·정리 스레드) |
| `src/livemax/supervisor.py` | 라이브별 프로세스 관리, 감시, 일일 재시작, 부팅 순차 시작 |
| `src/livemax/config.py` | 설정 기본값, 라이브 모델 (ID·포트·재시작 슬롯 자동) |
| `src/livemax/concat.py`, `liquidsoap.py`, `ffmpeg_cmd.py`, `tls_relay.py` | 송출 경로 |
| `src/livemax/probe.py`, `ingest.py`, `thumbs.py`, `catalog.py` | 업로드 검증·이동·썸네일 |
| `src/livemax/collector.py`, `sysmetrics.py`, `capacity.py` | 지표, "몇 개 더 가능" |
| `src/livemax/cleanup.py` | 자동 정리 (원본은 절대 삭제 안 함) |
| `src/livemax/web/api.py`, `auth.py`, `static/` | 웹 API, 로그인·세션, 화면 |
| `tools/windows/LiveMax-Convert.bat` | PC 미디어 변환기 (끌어다 놓기) |

## 개발

```bash
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
```

```bash
venv/bin/python -m pytest -q tests
```

로컬 실행 (Caddy 없이 http://127.0.0.1:8080):

```bash
LIVEMAX_DATA=/tmp/lmdata PYTHONPATH=src venv/bin/python -m livemax.daemon
```
