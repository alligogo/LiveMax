# LiveMax

유튜브 24/7 음악 라이브 다중 송출 시스템. 한 대의 VPS(Contabo Cloud VPS 4)에서 서로 다른 영상·음원 조합의 라이브 여러 개를 동시에, 무중단으로 송출한다.

설계 문서는 [DESIGN.md](DESIGN.md)에 있다. 코드는 그 설계를 그대로 따른다. 특히 **11장 "하지 말아야 할 것"** 을 코드 수준에서 강제한다.

---

## 핵심 원칙: 서버는 절대 재인코딩하지 않는다

| 구간 | 하는 일 | 재인코딩 여부 |
|---|---|---|
| 업로드 검증 | `ffprobe`로 헤더·패킷 플래그만 읽음 | 없음 (디코딩도 안 함) |
| 오디오 (Liquidsoap) | 폴더 감시·셔플·시간대 편성 후 `%ffmpeg(format="adts", %audio.copy)`로 출력 | 없음 (AAC 프레임 그대로) |
| 영상+먹싱 (ffmpeg) | concat 목록 + 오디오 → `-c:v copy -c:a copy` → RTMPS | 없음 |
| 썸네일 | 업로드 시 1회, 프레임 1장, 필터 없이 원본 해상도 JPEG | 송출과 무관 |
| 미리보기 | 유튜브 임베드 플레이어 | 서버 CPU 0 |

규칙을 지키게 만드는 장치는 다섯 가지다.

- **`ffmpeg_cmd.assert_copy_only()`**: 송출 직전에 명령줄 전체를 검사한다. `-vf`, `-filter_complex`, `-s`, `-r`, `-b:v`, `-preset`이 하나라도 있으면 실행하지 않는다. copy가 아닌 코덱이나 `drawtext`/`overlay`/`scale` 키워드가 있어도 마찬가지다.
- **`tests/test_no_reencode.py`**: 소스 전체를 AST로 훑는다. ffmpeg 필터 옵션 문자열이 어딘가에 있으면 테스트가 실패한다.
- **크로스페이드 금지**: `audio.crossfade`는 0만 허용한다. 오디오를 섞으려면 디코딩 후 재인코딩이 필요하기 때문이다(11-2).
- **`healthcheck.sh`**: 설치된 서버에서도 송출 명령이 copy 전용인지 다시 확인한다.
- **로그 마스킹**: 읽어 들인 스트림 키는 로그에서 자동으로 `****1a2b`처럼 가려진다.

---

## 아키텍처 (프로세스 구성)

```
사용자 PC ──(규격대로 인코딩)──▶ SFTP / 웹 업로드
                                    │
                         /media/_incoming/<video|audio>/<폴더>/
                                    │  livemax-ingest: ffprobe 검증 → rename (원자적)
                                    ▼
                  /media/video/<폴더>/      /media/audio/<폴더>/
                         │                         │
   라이브마다 (서로 완전 독립)                         │
   ┌───────────────────────────────────────────────────────────┐
   │ live-audio@ch01  Liquidsoap: 폴더 감시 + 셔플 + 시간대 편성     │
   │                  → harbor 127.0.0.1:8101 (AAC copy)          │
   │ live@ch01        runner: 24시간 concat 목록 생성                │
   │                  → ffmpeg -c copy 먹싱 → RTMPS → YouTube       │
   │                  → /run/livemax/ch01.status.json (speed 등)   │
   └───────────────────────────────────────────────────────────┘
   livemax-scheduler   라이브별 일일 재시작 (30초 이상 간격 강제)
   livemax-watchdog    speed / 출력 정지 / 유튜브 방송 상태 → 해당 라이브만 재시작
   livemax-collector   /proc + vnstat → SQLite (10초)
   livemax-dashboard   FastAPI (127.0.0.1:8080) ← Caddy HTTPS
   livemax-cleanup     하루 1회 (timer). 파생물·임시물만 정리
```

---

## 파일별 설명

### 루트

| 파일 | 역할 |
|---|---|
| `DESIGN.md` | 합의된 설계 문서 (원본) |
| `install.sh` | 빈 서버에 명령 하나로 설치. 멱등성 있음. 기존 설정·미디어·키는 덮어쓰지 않고, 실행 중인 라이브도 재시작하지 않음 |
| `healthcheck.sh` | 송출 준비 상태 점검 (바이너리, copy 가드, 설정, 키 권한, 디스크, 서비스, harbor 로컬 바인드) |
| `reset_password.py` | 대시보드 관리자 비밀번호를 랜덤으로 재설정 (분실 시) |
| `requirements.txt` | Python 의존성 |
| `assets/silence_10s.m4a` | 폴더가 비었을 때 내보내는 무음. 저장소에 **미리 인코딩된** 동일 스펙 파일 (서버에서 생성하지 않음) |
| `config.example/server.yaml` | 서버 공통 설정 예시 (경로, 임계값, 대시보드, 용량 가정치) |
| `config.example/ch01.yaml` | 라이브 1개 설정 예시 (DESIGN 5.1) |

### `src/livemax/` — 송출 파이프라인

| 파일 | 역할 |
|---|---|
| `config.py` | YAML 로딩·검증. 타임존 오프셋 숫자, 크로스페이드, 포트 중복을 거부함. **재시작 시각이 30초 이내로 겹치는지 1년치(서머타임 포함) 검사** |
| `paths.py` | 코드와 데이터 경로 분리 (DESIGN 10.2). 자동 정리 보호 경로 정의 |
| `probe.py` | ffprobe 규격 검증. 실패 사유를 사람이 읽는 문장으로 반환 (예: "키프레임 4.0초, 2초여야 함") |
| `ingest.py` | `_incoming` 감시 → 전송 완료 판정 → 검증 → `rename`으로 방송 폴더에 이동, 실패 시 `_rejected` 격리 |
| `thumbs.py` | 업로드 시 썸네일 1회 생성 |
| `catalog.py` | 폴더·파일 목록과 길이·비트레이트 캐시 (SQLite) |
| `concat.py` | **24시간치 concat 목록 생성.** 각 클립의 방송 시각을 타깃 시간대로 바꿔 구간 폴더에서 셔플백으로 고름 |
| `liquidsoap.py` | 라이브 YAML → Liquidsoap 2.2 스크립트 렌더링 (재생 방식 4종, 중복방지, 시간대 switch, copy 출력) |
| `audio.py` | `live-audio@<id>` 진입점. `.liq` 생성 후 `TZ=<지역명>`으로 liquidsoap을 exec |
| `ffmpeg_cmd.py` | 송출 명령 생성 + **재인코딩 차단 가드** |
| `runner.py` | `live@<id>` 진입점. concat 생성 → harbor 대기 → ffmpeg 실행 → `-progress` 파싱 → 상태 파일 기록 |
| `secrets.py` | 스트림 키 읽기(권한 600 확인), `****1a2b` 마스킹, 로그 자동 마스킹 필터 |

### `src/livemax/` — 운영

| 파일 | 역할 |
|---|---|
| `scheduler.py` | 계획된 일일 재시작. 라이브별 `restart_at`(라이브 시간대 기준), 전역 최소 간격 강제 |
| `watchdog.py` | speed 저하 지속, 출력 정지, 유튜브 방송 종료 감지 → 해당 라이브만 재시작. 시간당 재시작 한도 |
| `youtube.py` | YouTube Data API `videos.list` 방송 상태 확인 (호출당 1 unit) |
| `collector.py` | 시스템·라이브 지표 → SQLite. 디스크 90% 초과 시 긴급 정리 실행 |
| `sysmetrics.py` | `/proc` 읽기 (CPU, **steal time**, RAM, 디스크, 네트워크, 프로세스별 CPU·RSS), vnstat 월 누적 |
| `capacity.py` | 월 예상 트래픽, 32TB 대비 사용률, **"안전하게 몇 개 더 가능"** |
| `cleanup.py` | 자동 정리. `safe_unlink()`가 미디어 원본 경로의 삭제를 무조건 거부 |
| `systemctl.py` | 허용된 유닛만 `sudo -n systemctl`로 제어 |
| `db.py` | SQLite 스키마 (지표, 이벤트, 재시작, 카탈로그, 보안) |
| `state.py` | `/run/livemax/*.json` 원자적 읽기·쓰기 |
| `logsetup.py` | 상세 로그 (기본 DEBUG), logrotate 호환 핸들러, 마스킹 필터 |
| `cli.py` | 운영 CLI (`validate`, `probe`, `concat`, `liq`, `check-reencode` 등) |

### `src/livemax/web/` — 대시보드 (다크모드 전용)

| 파일 | 역할 |
|---|---|
| `app.py` | FastAPI 앱. 지역 차단 → CSRF(같은 출처) → 인증 → 비밀번호 변경 강제 미들웨어. 모니터링(`/`), `/api/status`, 수동 정리, 로그 조회 |
| `auth.py` | argon2 해시, 5회 실패 시 IP 15분 차단, DB 세션(토큰은 해시만 저장), 초기 비밀번호 |
| `geo.py` | 지역 차단 (기본 KR만), 임시 허용 IP, **SSH 터널은 항상 우회** |
| `data.py` | 상태 분류(정상/경고/중단/재시작중), 경고 목록, 디스크 내역 |
| `media_routes.py` | `/media/video`, `/media/audio`: 폴더 생성·이름변경·삭제, 스트리밍 업로드(스테이징 경유), 파일 삭제(영상은 오늘 편성이 쓰는 중이면 삭제 예약) |
| `live_routes.py` | `/live`, `/live/<id>`: 목록, 유튜브 임베드 미리보기, 설정 편집, 스트림 키 입력(마스킹 표시) |
| `settings_routes.py` | `/settings`: 비밀번호, 지역 차단, 허용 IP, 세션 강제 로그아웃, 로그인 기록, **설정 내보내기/가져오기(키 제외)** |
| `templates/*.html`, `static/` | 다크 전용 CSS, 모바일 반응형, 상태는 색 + 아이콘 + 라벨로 표시 |

### `systemd/`, `deploy/`

| 파일 | 역할 |
|---|---|
| `systemd/live@.service` | 라이브 영상 송출 템플릿 (`live@ch01`). `Restart=always`, `RestartSec=2` |
| `systemd/live-audio@.service` | 라이브 오디오 템플릿 (`live-audio@ch01`) |
| `systemd/livemax-{scheduler,watchdog,collector,ingest,dashboard}.service` | 공용 서비스. ingest와 dashboard는 `IOSchedulingClass=idle`(= `ionice -c3`) |
| `systemd/livemax-cleanup{,-emergency}.service`, `.timer` | 하루 1회 정리 / 디스크 90% 초과 시 긴급 정리 |
| `deploy/sudoers/livemax` | 서비스 계정이 LiveMax 유닛만 재시작할 수 있게 제한 |
| `deploy/logrotate/livemax` | 로그 로테이션 (일 단위, 14개, 200MB 상한) |
| `deploy/tmpfiles/livemax.conf` | `/run/livemax` 생성 |
| `deploy/caddy/Caddyfile.template` | HTTPS (Let's Encrypt) 리버스 프록시 |

### `tests/`

| 파일 | 검증 내용 |
|---|---|
| `test_no_reencode.py` | copy 가드, 금지 옵션 거부, 소스 전체 필터 문자열 스캔, Liquidsoap copy 출력 |
| `test_config.py` | 오프셋 타임존 거부, 서머타임 경계, **재시작 겹침(시간대가 달라도, 서머타임 기간에만 겹쳐도 검출)**, YAML `12:30` 함정 |
| `test_concat.py` | 정해진 현지 시각에 영상 폴더 전환, 24시간+여유 분량, 삭제 예약 제외 |
| `test_cleanup.py` | 원본 보호, 격리·고아 썸네일·지난 목록 정리 |
| `test_ops.py` | 워치독 판정, 용량 계산, 키 마스킹, **생성된 `.liq`가 실제 Liquidsoap에서 타입체크 통과** |

---

## 설치

### 요구사항

- Ubuntu **24.04 LTS** 이상. apt에 Liquidsoap 2.2.x가 있어야 한다. 22.04의 2.0.x는 지원하지 않는다.
- root 권한, SSH 공개키 등록. 설치 시 비밀번호 로그인을 끄므로 키가 먼저 등록되어 있어야 한다.
- (선택) 대시보드용 도메인 하나. DNS A 레코드가 서버 IP를 가리키면 된다.

### 설치 명령

```bash
git clone https://github.com/alligogo/LiveMax.git /opt/livemax
```

```bash
cd /opt/livemax && sudo ./install.sh
```

`install.sh`가 하는 일:

1. 사전 점검: OS, 디스크, RAM, 443 포트
2. 패키지 설치: ffmpeg, liquidsoap, vnstat, ufw, fail2ban, caddy 등. 이어서 버전 확인
3. `livemax` 서비스 계정 생성, venv 구성
4. `config/`, `secrets/`(700) 생성. 이미 있는 파일은 보존
5. `/media`, `/var/log/livemax`, `/var/lib/livemax` 생성. `_incoming`과 방송 폴더가 같은 파일시스템인지 확인
6. **랜덤 관리자 비밀번호 생성 후 출력.** `secrets/admin.initial_password`에도 저장됨
7. systemd 유닛, sudoers(visudo 검증), logrotate 등록
8. ufw로 SSH·443만 개방, fail2ban, unattended-upgrades 켜기. SSH 키 인증 전용 설정(키가 없으면 건너뜀). 도메인이 있으면 Caddy HTTPS 설정
9. 서비스 시작. 활성 라이브는 아직 안 돌고 있는 것만 31초 간격으로 시작

업데이트도 같은 방법이다. 설정·미디어·키는 보존된다.

```bash
cd /opt/livemax && git pull && sudo ./install.sh
```

### 설치 후 할 일

1. **대시보드 도메인 설정.** `config/server.yaml`의 `dashboard.domain`, `dashboard.acme_email`을 채우고 `sudo ./install.sh`를 다시 실행한다. 도메인이 없으면 SSH 터널로 접속한다.
   ```bash
   ssh -L 8080:127.0.0.1:8080 user@서버
   ```
   그다음 브라우저에서 http://localhost:8080 을 연다.
2. **첫 로그인.** 출력된 초기 비밀번호로 로그인하면 비밀번호 변경이 강제된다.
3. **지역 차단 DB 배치.** MaxMind GeoLite2(무료 가입 필요)의 `GeoLite2-Country.mmdb`를 `/var/lib/livemax/`에 둔다. 없으면 차단하지 않고 경고만 표시한다(`geo.fail_open`).
4. **(선택) 유튜브 상태 확인.** YouTube Data API 키를 `secrets/youtube_api.key`(600)에 넣고, 라이브 YAML의 `youtube.video_id`를 채운다.
5. 점검:
   ```bash
   sudo /opt/livemax/healthcheck.sh
   ```

---

## 사용법

### 라이브 추가 (코드 수정 없음, 다른 라이브 재시작 없음)

대시보드에서 하려면 **스트리밍 → + 라이브 추가**로 설정과 스트림 키를 저장한 뒤 서비스를 켠다.

```bash
sudo systemctl enable --now live-audio@ch02 live@ch02
```

손으로 하려면 다음 순서로 진행한다.

```bash
sudo -u livemax cp /opt/livemax/config/ch01.yaml.example /opt/livemax/config/ch02.yaml
```

```bash
sudo -u livemax nano /opt/livemax/config/ch02.yaml
```

id, `harbor_port`, `restart_at`, 편성을 수정한다.

```bash
echo '스트림키' | sudo -u livemax tee /opt/livemax/secrets/ch02.key >/dev/null && sudo chmod 600 /opt/livemax/secrets/ch02.key
```

```bash
sudo -u livemax env PYTHONPATH=/opt/livemax/src /opt/livemax/venv/bin/python -m livemax.cli validate
```

```bash
sudo systemctl enable --now live-audio@ch02 live@ch02
```

`validate`는 재시작 시각이 30초 이내로 겹치거나, 포트가 겹치거나, 타임존이 오프셋 숫자면 거부한다.

### 미디어 업로드

- **웹:** 영상/음원 페이지에서 폴더를 고르고 드래그앤드롭한다. 폰에서도 된다.
- **SFTP (대용량 권장):** `/media/_incoming/video/<폴더>/` 또는 `/media/_incoming/audio/<폴더>/`에 올린다.

어느 쪽이든 서버가 규격을 검증한다. 통과하면 방송 폴더로 원자적으로 이동하고, 실패하면 `_rejected/`로 격리된다. 사유는 대시보드에 표시된다. 방송 폴더에는 절대 직접 쓰지 않는다.

**규격** (DESIGN 2.2):

- 영상: H.264 High, 1920x1080, 30fps 고정, 5500k CBR, 키프레임 2초, B프레임 없음, 오디오 없음, MP4/MKV, 1분 클립
- 음원: AAC-LC 128k CBR, 44100Hz, 스테레오, -14 LUFS, M4A

인코딩은 사용자 PC에서 한다. 서버에서는 하지 않는다. 아래는 참고용 ffmpeg 예시다.

영상 클립:

```bash
ffmpeg -i src.mov -t 60 -an -c:v libx264 -profile:v high -preset slow -r 30 -s 1920x1080 -b:v 5500k -minrate 5500k -maxrate 5500k -bufsize 5500k -x264-params nal-hrd=cbr -g 60 -keyint_min 60 -sc_threshold 0 -bf 0 -movflags +faststart clip.mp4
```

음원 (2-pass loudnorm은 생략한 단순 예시):

```bash
ffmpeg -i song.wav -af loudnorm=I=-14:TP=-1:LRA=11 -ar 44100 -ac 2 -c:a aac -profile:a aac_low -b:a 128k song.m4a
```

업로드 전에 규격을 확인할 수 있다.

```bash
/opt/livemax/venv/bin/python -m livemax.cli probe video clip.mp4
```

### 운영 명령

| 목적 | 명령 |
|---|---|
| 전체 점검 | `sudo /opt/livemax/healthcheck.sh` |
| 라이브 상태 | `systemctl status live@ch01 live-audio@ch01` |
| 송출 로그 | `tail -f /var/log/livemax/live-ch01.log` |
| 오디오 로그 | `tail -f /var/log/livemax/audio-ch01.log` |
| 오늘의 편성 목록 | `ls /var/lib/livemax/concat/ch01/` |
| 수동 정리 | 대시보드 유지관리의 "지금 정리 실행" 또는 `sudo systemctl start livemax-cleanup` |
| 비밀번호 분실 | `sudo -u livemax /opt/livemax/venv/bin/python /opt/livemax/reset_password.py` |
| 라이브 1개 중지·삭제 | `sudo systemctl disable --now live@ch05 live-audio@ch05` 후 `config/ch05.yaml` 삭제 |

### 새 서버(2번 서버) 구축

`install.sh` → 대시보드에서 **설정 → 설정 가져오기**(1번 서버에서 내보낸 zip) → 라이브마다 스트림 키 입력·활성화 → 미디어 업로드.

내보내기 파일에는 스트림 키가 들어 있지 않다. 그래서 가져온 라이브는 비활성 상태로 들어온다.

---

## 개발·테스트

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

```bash
venv/bin/python -m pytest -q tests
```

`liquidsoap`이 설치되어 있으면 생성된 스크립트의 실제 타입체크 테스트도 함께 돈다.

---

## 설계와 구현 사이에서 내린 결정

DESIGN.md와 다르거나 문서에 없던 부분이다. 바꾸고 싶으면 알려주면 된다.

1. **크로스페이드 비활성화 (`crossfade: 0` 강제).** DESIGN 5.1 예시에는 `crossfade: 3`이 있다. 하지만 오디오 copy 방식에서는 곡을 섞을 수 없다. 섞으려면 Liquidsoap이 디코딩 후 AAC로 재인코딩해야 하고, 이는 11-2 "서버에서 인코딩하지 않는다"에 위배된다. 필요하면 곡 파일에 페이드를 넣어서 업로드한다.
2. **라이브 설정은 `config/`에 있고 git에서 제외된다.** DESIGN 5.1은 "커밋함", 10.2와 `.gitignore`는 "git 제외"로 서로 다르다. 저장소의 `.gitignore`를 따랐다. 커밋하는 것은 `config.example/`뿐이다. 설정 백업은 대시보드 내보내기로 한다.
3. **`restart_at`은 라이브의 `timezone` 기준 시각이다.** 겹침 검사는 UTC 순간으로 1년치를 비교한다. 서로 다른 시간대의 라이브가 서머타임 기간에만 겹치는 경우도 잡는다.
4. **비트레이트 최소 5000k는 업로드 검증 기준이다.** 라이브 설정값은 막지 않는다. DESIGN 13의 4500k 비교 실험을 하려면 `server.yaml`의 `spec.video.min_bitrate`를 일시적으로 낮추면 된다. copy 송출이므로 실제 송출 비트레이트는 클립 자체의 값이다. 설정값은 경고와 용량 계산에 쓰인다.
5. **일일 재시작은 영상(ffmpeg)만 대상이다.** Liquidsoap은 계속 돌아서 중복방지 기록과 곡 흐름이 끊기지 않는다.
6. **ffmpeg가 시작될 때마다 "지금부터" 24시간 목록을 새로 만든다.** 장애 재시작 뒤에도 시간대 편성이 정확하다.
7. **라우드니스(-14 LUFS)는 서버에서 검사하지 않는다.** 측정하려면 전체 디코딩이 필요하다. 사용자 PC 인코딩 단계의 책임으로 둔다.
8. **사용 중인 영상 클립의 삭제는 "삭제 예약"이다.** 오늘 편성 목록이 참조하는 클립을 바로 지우면 ffmpeg가 그 지점에서 멈춘다. 참조가 끝나면 cleanup이 지운다.
9. **방화벽은 22·443만 연다.** Let's Encrypt는 443의 TLS-ALPN 챌린지로 발급받는다.

## 현재 상태 (뼈대)

DESIGN 12장의 1~11단계 구성요소를 모두 배치했다. 컨테이너에서 검증한 범위와 아직 안 된 범위는 다음과 같다.

**확인됨**

- 규격 검증과 거부 사유
- 원자적 이동
- Liquidsoap 2.2.4 copy 출력
- 방송 중 곡 추가의 무중단 반영
- 시간대 구간 선택
- ffmpeg copy 먹싱 결과물: H.264 High + AAC LC, speed ≈1.0
- 로그 키 마스킹
- 대시보드 인증·차단·CSRF·내보내기 키 제외
- 테스트 56개

**미확인**

- 실제 유튜브 RTMPS 송출. 출력은 로컬 FLV 파일로만 확인했다.
- systemd 실기동과 `install.sh` 전체 실행. 컨테이너에 systemd가 없다.
- 72시간 안정성 (DESIGN 12-2단계)
- YouTube API 연동
- GeoIP 차단

**DESIGN 12장 순서를 지킬 것:** 1개 라이브로 72시간 안정성을 확인하기 전에는 2개 이상으로 늘리지 않는다.
