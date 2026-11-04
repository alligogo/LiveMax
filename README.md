# LiveMax

유튜브 24/7 음악 라이브 다중 송출 시스템. 서버 한 대(Contabo Cloud VPS 4)에서 서로 다른 비디오·음악 조합의 라이브 여러 개를 동시에, 무중단으로 송출한다. 설계는 [DESIGN.md](DESIGN.md).

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
                    라이브마다 독립 프로세스:  Liquidsoap(음악) → ffmpeg(비디오+먹싱, copy) → stunnel(TLS)
                    감시 · 타이머 · 일일 재시작(라이브마다 2분 간격) · 업로드 검증 · 지표 · 자동 정리 · 모바일 알림

데이터 /opt/livemax/data  (컨테이너의 /data)
  config/lives/live1.yaml …   라이브 설정 (웹에서 관리)
  secrets/                    스트림 키, 관리자 비밀번호 해시, Pushbullet 토큰 (700)
  media/_incoming/…           업로드 스테이징 (SFTP 로 올릴 때도 여기)
  media/video|audio/<폴더>/   방송 원본
  state/ (DB·목록·썸네일·인증서), logs/
```

### 송출 경로 — 서버는 재인코딩하지 않는다 (DESIGN 11-1, 11-2)

| 구간 | 하는 일 |
|---|---|
| 업로드 | ffprobe 로 헤더·패킷만 검사 → 통과하면 원자적으로 방송 폴더로 이동, 실패하면 이유와 함께 격리 |
| 음악 (Liquidsoap) | 고른 폴더들의 전체 곡 목록(`run/<id>.m3u`) 하나를 곡 단위로 셔플 → `%audio.copy` (AAC 프레임 그대로). 크로스페이드를 켠 라이브만 오디오 재인코딩 |
| 비디오 (ffmpeg) | 24시간 목록 + 음악 → `-c:v copy -c:a copy` → 로컬 RTMP |
| TLS (stunnel) | RTMPS 암호화를 큰 레코드로 → 유튜브 `a.rtmps.youtube.com` (IPv4 고정, 인증서 검증) |

`ffmpeg_cmd.assert_copy_only()`가 실행 직전에 필터·인코더 옵션을 검사하고, 테스트가 소스 전체를 스캔해 필터 문자열을 막는다.

### 타이머 (자동 종료) — 12시간 다시보기 만들기

라이브 설정 → **타이머**: `방송 시간`(예: 11시간 50분) 또는 `종료 시각`(날짜+시간)을 고른다.

- 유튜브 스튜디오의 스트림 설정에서 **'자동 시작'과 '자동 중지 사용'**을 켜 두면, 송출이 멈출 때 방송이 끝나고 다시보기 영상으로 남는다.
- 방송 시간은 **시작 버튼을 누른 시각**부터 잰다. 감시·재시작 버튼으로 잠깐 끊겨도 같은 방송으로 계속 센다 (`state/run/<id>.session.json`).
- **반복**을 켜면 끝난 뒤 N분(기본 2분) 쉬었다가 새 방송으로 다시 시작한다 → 12시간짜리 다시보기가 계속 쌓인다.
- 타이머를 켠 라이브는 일일 자동 재시작을 건너뛴다 (재시작 공백 때문에 방송이 일찍 끝나지 않게).
- LiveMax 업데이트처럼 5분 안에 다시 켜지면 같은 방송으로 이어 가고, 더 오래 꺼져 있었으면 새 방송으로 시작한다.

### 재시작 공백

| 동작 | 다시 띄우는 것 | 공백 |
|---|---|---|
| 재시작 버튼, 감시 자동 재시작, 일일 재시작 | 비디오(ffmpeg)만 — 음악은 계속 흐름 | 약 2~3초 |
| 비디오 폴더·스트림 키 변경 후 '지금 적용' | 비디오만 | 약 2~3초 |
| 재생 방식·중복방지 곡 수·크로스페이드 변경 후 '지금 적용' | 음악+비디오 (Liquidsoap 기동 5~7초) | 약 8초 |
| 음악 폴더·이름·타이머·유튜브 ID 변경 | 없음 (음악 폴더는 다음 곡부터 반영) | 0 |
| 유튜브 쪽에서 연결이 끊김 | 비디오만 1초 뒤 재연결 | 약 2초 |

### 모바일 알림 (Pushbullet)

설정 → **모바일 알림**에 Pushbullet Access Token 을 넣고 보낼 알림 종류를 고른다:
송출 중단·오류(1분 이상) / 복구 / 자동 재시작 / 타이머 / 서버(디스크·재부팅·LiveMax 시작) / 보안(로그인 차단) / 업로드 검증 실패.
같은 내용은 10분에 한 번, 시간당 20건까지만 보낸다 (Pushbullet 무료는 월 500건). 토큰은 `secrets/pushbullet.token`(600)에만 저장되고 화면·로그에는 `****ab12`로만 보인다.

### 음악 랜덤 재생

여러 음악 폴더를 고르면 **전체 곡을 한 목록으로** 섞는다 (39곡 폴더 + 3곡 폴더 = 42곡 중 랜덤, 폴더별 가중치 없음).
중복방지 N곡: 최근 N곡은 다시 고르지 않는다. 곡이 적으면 창을 전체 곡 수의 절반까지로 줄여 같은 곡이 연달아 나오지 않게 한다.
곡을 추가·삭제하면 15초 안에 목록이 갱신되고, 재시작 없이 다음 곡부터 반영된다.

### 라이브 삭제

라이브를 지우면 스트림 키, 설정, 실행 파일(`run/<id>.*`), 영상 목록, 로그(`live-/audio-/tls-<id>.log`), 지표·이벤트 기록을 함께 지운다.

### 미디어 삭제

라이브가 쓰고 있는 폴더의 파일은 지울 수 없다 (라이브 설정에서 폴더를 빼고 지운다). 그 외에는 바로 삭제된다.
용량 계산의 라이브당 비트레이트는 송출 중이면 실측값, 아니면 고른 비디오 폴더 파일들의 평균을 쓴다 (별도 설정 없음).

---

## v1 운영에서 배운 것 (코드에 반영됨)

| 문제 | 원인 | v2 |
|---|---|---|
| 송신이 예상보다 48% 많음 (8.4 Mbps) | ffmpeg 내장 RTMPS 가 128바이트마다 TLS 레코드를 만듦 | 라이브별 stunnel 이 큰 레코드로 → **5.9 Mbps** |
| 10분마다 송출 끊김 | 유튜브행 IPv6 경로가 주기적으로 막힘 | stunnel IPv4 고정 + 서버 IPv4 우선 + keepalive |
| 인증서 검증 실패 | RTMPS 호스트를 `a.rtmp.youtube.com`으로 씀 | 정식 `a.rtmps.youtube.com` |
| 대시보드 접속에 도스창·터널 필요 | localhost 바인드만 | `https://서버IP` 정식 인증서 |
| 설치가 복잡 (bootstrap·deploy key·install) | 서버에 직접 설치 | Docker + 명령 1줄, 저장소 공개 |
| 저장해도 송출 중엔 반영 안 됨 | 시작할 때만 설정을 읽음 | **저장하고 지금 적용** (바뀐 부분만, 그 라이브만) |
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
| `src/livemax/supervisor.py` | 라이브별 프로세스 관리, 감시, 타이머(방송 세션), 일일 재시작, 부팅 순차 시작 |
| `src/livemax/notify.py` | Pushbullet 모바일 알림 (종류 선택·중복 억제·시간당 한도) |
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
