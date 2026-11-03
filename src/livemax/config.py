"""설정 로딩·검증.

- server.yaml : 서버 공통 설정 (경로, 임계값, 대시보드, 용량 가정치)
- <id>.yaml   : 라이브 1개 설정 (DESIGN 5.1). 라이브 추가 = 파일 1개 추가.

코드에는 라이브 관련 상수가 없다 (DESIGN 1.3, 11-5, 11-10).
비트레이트·스트림 키·라이브 수는 전부 이 파일들에서 온다.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import paths as _paths

SERVER_FILE = "server.yaml"
LIVE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
AUDIO_MODES = ("random", "random_norepeat", "sequential", "sequential_loop")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# server.yaml
# ---------------------------------------------------------------------------

# 서버 공통 기본값. 서버 고유값(IP, 호스트명, 라이브 개수, 키)은 여기 두지 않는다.
DEFAULT_SERVER: dict[str, Any] = {
    "paths": {
        "media_root": "/media",
        "log_dir": "/var/log/livemax",
        "run_dir": "/run/livemax",
        "state_dir": "/var/lib/livemax",
        "db": "/var/lib/livemax/livemax.db",
    },
    "logging": {"level": "DEBUG"},  # 처음엔 과하다 싶을 만큼 상세하게 (DESIGN 6.3)
    # 업로드 규격 (DESIGN 2.2). 라이브별 송출 비트레이트와는 별개인 "입구 검증" 기준.
    "spec": {
        "video": {
            "codec": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "min_bitrate": "5000k",
            "keyframe_interval": 2.0,
            "keyframe_tolerance": 0.05,
            "containers": ["mp4", "mkv"],
        },
        "audio": {
            "codec": "aac",
            "profile": "LC",
            "bitrate": "128k",
            "bitrate_tolerance": 0.10,
            "sample_rate": 44100,
            "channels": 2,
            "containers": ["m4a", "aac"],
        },
    },
    "ingest": {
        "poll_seconds": 5,
        "stable_seconds": 15,  # 파일 크기·mtime 이 이 시간 동안 안 바뀌면 전송 완료로 본다
    },
    "output": {
        # 유튜브 RTMPS 인제스트 주소 (서버 고유값 아님). 나중에 MediaMTX 를 끼우면 여기만 바꾼다.
        # 주의: RTMPS 는 a.rtmps.youtube.com (rtmp"s"). a.rtmp.youtube.com 은 RTMP 용 호스트라
        #       인증서 이름(rtmps.youtube.com)이 맞지 않아 검증하는 클라이언트(stunnel)는 거부한다.
        "default_url": "rtmps://a.rtmps.youtube.com/live2",
        "audio_connect_timeout": 90,
        # RTMPS 암호화를 ffmpeg 대신 라이브별 stunnel 이 큰 TLS 레코드로 처리 → 송출 트래픽 약 30% 절감
        # (tls_relay.py 참고). 띄우지 못하면 직접 RTMPS 로 자동 전환.
        "tls_relay": {"enabled": True, "verify": True},
    },
    "concat": {
        "hours": 24,
        "margin_minutes": 45,  # 다음 일일 재시작까지 목록이 바닥나지 않게 여유
    },
    "restart": {"min_gap_seconds": 30},  # 라이브 간 재시작 최소 간격 (DESIGN 4.4, 6.1)
    "watchdog": {
        "poll_seconds": 5,
        "speed_min": 0.97,
        "speed_grace_seconds": 60,
        "stall_seconds": 30,
        "startup_grace_seconds": 90,
        "max_restarts_per_hour": 6,
        "restart_spacing_seconds": 5,
        "youtube_check_seconds": 300,
        "youtube_fail_threshold": 2,
    },
    "collector": {
        "interval_seconds": 10,
        "network_interface": "auto",
        "vnstat_interval_seconds": 300,
    },
    "cleanup": {
        "rejected_days": 7,
        "incoming_stale_hours": 24,
        "log_days": 14,
        "metrics_raw_days": 30,
        "disk_warn_percent": 80,
        "disk_emergency_percent": 90,
    },
    "capacity": {
        "monthly_traffic_tb": 32,  # FUP 추정 안전선 (DESIGN 2.1) — 실측 후 교정
        "port_mbit": 200,
        "safety_ratio": 0.85,
        "audio_kbps": 128,
        "overhead_ratio": 0.03,
        "cpu_percent_total": 400,
        "ram_mb_per_live": 450,
    },
    "dashboard": {
        "username": "admin",
        "bind": "127.0.0.1",
        "port": 8080,
        "domain": "",  # 비우면 Caddy(HTTPS) 설정을 만들지 않는다
        "session_hours": 72,
        "login_max_failures": 5,
        "login_block_minutes": 15,
        "max_upload_mb": 4096,
    },
    "geo": {
        "enabled": True,  # 기본 ON, 국내만 허용 (DESIGN 9.3)
        "allowed_countries": ["KR"],
        "mmdb_path": "/var/lib/livemax/GeoLite2-Country.mmdb",
        "fail_open": True,  # DB 가 없을 때 차단하지 않고 경고만 (잠김 방지)
    },
    "youtube": {"api_key_file": "secrets/youtube_api.key"},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_server(config_dir: Path | None = None) -> dict:
    config_dir = config_dir or _paths.CONFIG_DIR
    f = config_dir / SERVER_FILE
    data: dict = {}
    if f.exists():
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return _deep_merge(DEFAULT_SERVER, data)


def get_paths(server: dict | None = None) -> _paths.Paths:
    return _paths.from_server_config(server or load_server())


# ---------------------------------------------------------------------------
# 라이브 설정
# ---------------------------------------------------------------------------


def parse_bitrate(v: Any) -> int:
    """'5500k' / '5.5M' / 5500 → kbps(int)."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)(?:bps|b)?", s)
    if not m:
        raise ConfigError(f"비트레이트 형식이 잘못됨: {v!r} (예: 5500k)")
    num, unit = float(m.group(1)), m.group(2)
    if unit == "m":
        return int(num * 1000)
    if unit == "k":
        return int(num)
    return int(num / 1000)  # 단위 없음 = bps


def norm_hhmm(v: Any) -> str:
    """YAML 1.1 함정 보정: 따옴표 없는 `12:30` 은 PyYAML 이 60진수 정수 750 으로 읽는다.
    정수로 들어오면 다시 'HH:MM' 으로 되돌린다."""
    if isinstance(v, int) and not isinstance(v, bool):
        if 0 <= v < 24 * 60:
            return f"{v // 60:02d}:{v % 60:02d}"
        raise ConfigError(f"시각 형식이 잘못됨: {v!r} (HH:MM, 따옴표로 감싸 주세요)")
    return str(v).strip()


def hhmm_to_minutes(s: Any) -> int:
    s = norm_hhmm(s)
    m = HHMM_RE.match(s)
    if not m:
        raise ConfigError(f"시각 형식이 잘못됨: {s!r} (HH:MM)")
    return int(m.group(1)) * 60 + int(m.group(2))


# 폴더 이름은 Liquidsoap 스크립트 문자열·ffconcat 목록에 그대로 들어가므로 안전한 문자만 허용한다.
# 첫 글자: 문자/숫자 ('_' '.' 로 시작하는 이름은 _incoming 같은 시스템 예약), 이후: 문자/숫자/공백/._-()
FOLDER_RE = re.compile(r"^[^\W_][\w .()\-]{0,63}$")


def valid_folder_name(name: str) -> bool:
    return bool(FOLDER_RE.fullmatch(name or "")) and ".." not in name


@dataclass
class Segment:
    start_min: int  # 타깃 시간대 기준 자정 이후 분
    start: str  # "06:00"
    video: list[str]
    audio: list[str]


@dataclass
class LiveConfig:
    id: str
    name: str
    enabled: bool
    bitrate_kbps: int
    audio_mode: str
    norepeat_count: int
    crossfade: float
    harbor_port: int
    timezone: str
    schedule: list[Segment]
    restart_at: str
    stream_key_file: Path
    youtube_video_id: str = ""
    output_url: str = ""
    path: Path | None = None
    raw: dict = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def restart_minutes(self) -> int:
        return hhmm_to_minutes(self.restart_at)

    def video_folders(self) -> set[str]:
        return {f for s in self.schedule for f in s.video}

    def audio_folders(self) -> set[str]:
        return {f for s in self.schedule for f in s.audio}

    def segment_at(self, when_utc: datetime) -> Segment:
        """주어진 UTC 시각에 해당하는 편성 구간. 서머타임은 zoneinfo 가 계산한다."""
        local = when_utc.astimezone(self.tz)
        minute = local.hour * 60 + local.minute
        return segment_for_minute(self.schedule, minute)

    def restart_instant_utc(self, local_day: date) -> datetime:
        """타깃 시간대의 local_day 날짜 restart_at 시각을 UTC 로."""
        h, m = divmod(self.restart_minutes, 60)
        local = datetime.combine(local_day, time(h, m), tzinfo=self.tz)
        return local.astimezone(timezone.utc)


def segment_for_minute(schedule: list[Segment], minute: int) -> Segment:
    """구간은 start 오름차순. 자정 이전 첫 구간 전 시간은 마지막 구간(전날 밤에서 이어짐)."""
    current = schedule[-1]
    for seg in schedule:
        if seg.start_min <= minute:
            current = seg
        else:
            break
    return current


def resolve_home_path(s: str) -> Path:
    """'secrets/ch01.key' → SECRETS_DIR/ch01.key, 그 외 상대경로는 코드 루트 기준."""
    p = Path(s)
    if p.is_absolute():
        return p
    if p.parts and p.parts[0] == "secrets":
        return _paths.SECRETS_DIR.joinpath(*p.parts[1:])
    return _paths.HOME / p


def _req(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"{where}: '{key}' 항목이 없음")
    return d[key]


def parse_live(data: dict, path: Path | None = None, server: dict | None = None) -> LiveConfig:
    where = str(path) if path else "live config"
    server = server or DEFAULT_SERVER
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: YAML 최상위가 매핑이 아님")

    live_id = str(_req(data, "id", where))
    if not LIVE_ID_RE.match(live_id):
        raise ConfigError(f"{where}: id '{live_id}' 형식 오류 (소문자/숫자/_/-)")
    if path is not None and path.stem != live_id:
        raise ConfigError(f"{where}: 파일명({path.stem})과 id({live_id})가 같아야 함")

    video = data.get("video") or {}
    audio = data.get("audio") or {}

    bitrate = parse_bitrate(_req(video, "bitrate", f"{where} video"))
    # 업로드 최소값(spec.video.min_bitrate=5000k)은 입구 검증 기준이고, 라이브 설정값은 막지 않는다.
    # DESIGN 13 의 4500k/5500k 비교 실험을 하려면 라이브마다 다른 값이 필요하기 때문.
    if not (1000 <= bitrate <= 51000):
        raise ConfigError(f"{where}: video.bitrate {bitrate}k 범위 오류 (1000k~51000k)")

    mode = audio.get("mode", "random_norepeat")
    if mode not in AUDIO_MODES:
        raise ConfigError(f"{where}: audio.mode '{mode}' 은 {AUDIO_MODES} 중 하나여야 함")
    norepeat = int(audio.get("norepeat_count", 30))
    if norepeat < 0:
        raise ConfigError(f"{where}: audio.norepeat_count 는 0 이상")

    # 크로스페이드(초). 기본 0 = 오디오 copy (디코딩 없음).
    # > 0 이면 그 라이브만 Liquidsoap 이 오디오를 디코딩→섞기→AAC 재인코딩한다 (영상은 계속 copy).
    # DESIGN 11-1/11-2 의 재인코딩 금지는 영상 대상이므로 허용한다.
    try:
        crossfade = float(audio.get("crossfade", 0) or 0)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: audio.crossfade 는 초 단위 숫자여야 함") from e
    if not (0 <= crossfade <= 10):
        raise ConfigError(f"{where}: audio.crossfade={crossfade} 범위 오류 (0~10초)")

    port = int(_req(audio, "harbor_port", f"{where} audio"))
    if not (1024 < port < 65536):
        raise ConfigError(f"{where}: audio.harbor_port {port} 범위 오류 (1025~65535)")

    tzname = str(_req(data, "timezone", where))
    if re.fullmatch(r"(?i)(utc|gmt)?\s*[+-]\s*\d{1,2}(:?\d{2})?", tzname) or tzname.upper().startswith(("UTC+", "UTC-", "GMT+", "GMT-", "ETC/GMT")):
        # DESIGN 4.5, 11-4: 오프셋 숫자 금지. 지역명만.
        raise ConfigError(f"{where}: timezone '{tzname}' — UTC 오프셋 숫자 금지. 'America/Chicago' 같은 지역명을 쓰세요")
    try:
        ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"{where}: timezone '{tzname}' 을 찾을 수 없음") from e

    raw_sched = _req(data, "schedule", where)
    if not isinstance(raw_sched, list) or not raw_sched:
        raise ConfigError(f"{where}: schedule 은 1개 이상의 구간 목록이어야 함")
    segments: list[Segment] = []
    for i, s in enumerate(raw_sched):
        sw = f"{where} schedule[{i}]"
        start = norm_hhmm(_req(s, "from", sw))
        vids = s.get("video") or []
        auds = s.get("audio") or []
        if isinstance(vids, str):
            vids = [vids]
        if isinstance(auds, str):
            auds = [auds]
        if not vids or not auds:
            raise ConfigError(f"{sw}: video 와 audio 폴더를 각각 1개 이상 지정해야 함")
        for f in [*vids, *auds]:
            if not valid_folder_name(str(f)):
                raise ConfigError(f"{sw}: 폴더 이름 '{f}' 사용 불가")
        segments.append(Segment(hhmm_to_minutes(start), start, [str(v) for v in vids], [str(a) for a in auds]))
    segments.sort(key=lambda x: x.start_min)
    if len({s.start_min for s in segments}) != len(segments):
        raise ConfigError(f"{where}: schedule 에 같은 시작 시각이 중복됨")

    restart_at = norm_hhmm(_req(data, "restart_at", where))
    hhmm_to_minutes(restart_at)

    key_file = resolve_home_path(str(_req(data, "stream_key_file", where)))

    yt = data.get("youtube") or {}
    out = data.get("output") or {}

    return LiveConfig(
        id=live_id,
        name=str(data.get("name", live_id)),
        enabled=bool(data.get("enabled", True)),
        bitrate_kbps=bitrate,
        audio_mode=mode,
        norepeat_count=norepeat,
        crossfade=crossfade,
        harbor_port=port,
        timezone=tzname,
        schedule=segments,
        restart_at=restart_at,
        stream_key_file=key_file,
        youtube_video_id=str(yt.get("video_id", "") or ""),
        output_url=str(out.get("url", "") or ""),
        path=path,
        raw=data,
    )


def live_files(config_dir: Path | None = None) -> list[Path]:
    config_dir = config_dir or _paths.CONFIG_DIR
    if not config_dir.is_dir():
        return []
    return sorted(
        p for p in config_dir.glob("*.yaml") if p.name != SERVER_FILE and not p.name.startswith(".")
    )


def load_live(live_id: str, config_dir: Path | None = None, server: dict | None = None) -> LiveConfig:
    config_dir = config_dir or _paths.CONFIG_DIR
    p = config_dir / f"{live_id}.yaml"
    if not p.exists():
        raise ConfigError(f"라이브 설정 파일 없음: {p}")
    return parse_live(yaml.safe_load(p.read_text(encoding="utf-8")), p, server)


def load_all_lives(config_dir: Path | None = None, server: dict | None = None,
                   cross: bool = True) -> tuple[list[LiveConfig], dict[str, str]]:
    """모든 라이브 설정. 하나가 깨져도 나머지는 계속 동작해야 하므로 오류는 따로 모은다.
    cross=False 면 라이브 간 충돌 검사(1년치 재시작 시각 비교)를 생략한다 (10초 주기 루프용)."""
    lives: list[LiveConfig] = []
    errors: dict[str, str] = {}
    for p in live_files(config_dir):
        try:
            lives.append(parse_live(yaml.safe_load(p.read_text(encoding="utf-8")), p, server))
        except Exception as e:  # noqa: BLE001 — 설정 하나의 오류가 전체를 죽이면 안 됨
            errors[p.name] = str(e)
    for msg in (cross_check(lives, server) if cross else []):
        errors.setdefault("_cross", "")
        errors["_cross"] += msg + "\n"
    return lives, errors


def cross_check(lives: list[LiveConfig], server: dict | None = None, days: int = 366) -> list[str]:
    """라이브 간 충돌 검사: id 중복, harbor 포트 중복, 재시작 시각 간격 (DESIGN 4.4, 11-6).

    라이브마다 시간대가 다를 수 있고 서머타임 때문에 UTC 기준 간격이 날짜마다 바뀌므로
    앞으로 1년치 날짜를 모두 검사한다.
    """
    server = server or DEFAULT_SERVER
    msgs: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_ports: dict[int, str] = {}
    reserved = {int(server["dashboard"]["port"])}
    for lv in lives:
        if lv.id in seen_ids:
            msgs.append(f"id 중복: {lv.id}")
        seen_ids[lv.id] = lv.id
        if lv.harbor_port in seen_ports:
            msgs.append(f"harbor_port {lv.harbor_port} 중복: {seen_ports[lv.harbor_port]}, {lv.id}")
        if lv.harbor_port in reserved:
            msgs.append(f"{lv.id}: harbor_port {lv.harbor_port} 는 대시보드 포트와 겹침")
        seen_ports[lv.harbor_port] = lv.id

    gap = int(server["restart"]["min_gap_seconds"])
    active = [lv for lv in lives if lv.enabled]
    if len(active) > 1:
        today = datetime.now(timezone.utc).date()
        reported: set[tuple[str, str]] = set()
        for d in range(days):
            day = today + timedelta(days=d)
            # 날짜 경계를 넘는 경우(23:59:50 vs 00:00:10)도 잡기 위해 같은 날의 UTC 순간을 원형으로 비교
            instants = sorted((lv.restart_instant_utc(day).timestamp() % 86400, lv.id) for lv in active)
            for i, (t, lid) in enumerate(instants):
                t2, lid2 = instants[(i + 1) % len(instants)]
                diff = (t2 - t) % 86400 if len(instants) > 1 else 86400
                pair = tuple(sorted((lid, lid2)))
                if lid != lid2 and diff < gap and pair not in reported:
                    reported.add(pair)
                    msgs.append(
                        f"재시작 시각이 {gap}초 이내로 겹침: {pair[0]} / {pair[1]} ({day.isoformat()} 기준 {int(diff)}초 차이)"
                    )
    return msgs


def dump_live_yaml(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None)
