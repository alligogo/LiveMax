"""설정 (v2) — 사용자가 고르는 것은 최소로, 나머지는 자동.

라이브 1개 설정에서 사람이 정하는 것: 이름, 스트림 키, 영상 폴더(체크), 음원 폴더(체크), 재생 방식.
자동으로 정해지는 것: ID(live1, live2 …), 오디오 내부 포트, 일일 재시작 시각(라이브마다 겹치지 않게).
시간대 편성·구간·타깃 시간대는 v1 운영 결과 불필요해 제거했다.

코드에는 라이브 관련 상수가 없다 (DESIGN 1.3, 11-5, 11-10). 비트레이트·키·라이브 수는 전부 데이터.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .paths import Paths

AUDIO_MODES = ("random_norepeat", "random", "sequential_loop", "sequential")
LIVE_ID_RE = re.compile(r"^live([1-9][0-9]{0,3})$")
FOLDER_RE = re.compile(r"^[^\W_][\w .()\-]{0,63}$")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# 서버 설정 기본값 (data/config/settings.yaml 로 덮어쓸 수 있음)
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "logging": {"level": "INFO"},
    "spec": {
        # 기본은 1080p 30fps 5000k. 720p~4K, 24~60fps 도 받는다 (송출은 파일 그대로 copy).
        # 단, 한 라이브 안에서는 해상도·fps 를 통일해야 한다 (섞이면 유튜브 화면이 깨질 수 있음 — 화면에서 경고).
        "video": {"codec": "h264", "profile": "High", "resolutions": [[1280, 720], [1920, 1080], [2560, 1440], [3840, 2160]],
                  "fps": [24, 25, 30, 50, 60], "min_bitrate": "5000k", "keyframe_interval": 2.0, "keyframe_tolerance": 0.05,
                  "containers": ["mp4", "mkv"]},
        # bitrate 는 크로스페이드를 켠 라이브가 다시 압축할 때 쓰는 값. 업로드는 128k~192k 를 받는다 (송출은 파일 그대로)
        "audio": {"codec": "aac", "profile": "LC", "bitrate": "128k", "bitrate_min": "128k", "bitrate_max": "192k",
                  "bitrate_tolerance": 0.10,
                  "sample_rate": 44100, "channels": 2, "containers": ["m4a", "aac"]},
    },
    "ingest": {"poll_seconds": 3, "stable_seconds": 10},
    "output": {
        # 유튜브 RTMPS 인제스트. RTMPS 는 a.rtmps.youtube.com (rtmp"s") — 인증서 이름이 맞는 호스트.
        "default_url": "rtmps://a.rtmps.youtube.com/live2",
        # 라이브별 stunnel 이 TLS 를 큰 레코드로 처리 → 송출 트래픽 약 25% 절감 (v1 live1 실측 8.0→5.9 Mbps)
        # ipv4: v1 live1 에서 유튜브행 IPv6 경로가 약 10분마다 끊겨 송출이 멈췄다 → IPv4 고정.
        "tls_relay": {"enabled": True, "verify": True, "ipv4": True},
        "audio_connect_timeout": 60,
    },
    "concat": {"hours": 24, "margin_minutes": 45},
    "supervisor": {
        "boot_start_gap_seconds": 10,   # 컨테이너 시작 시 라이브 간 시작 간격
        "restart_backoff_seconds": [1, 3, 10, 30, 60],  # 오래 잘 돌다 끊긴 경우 1초 뒤 재연결
    },
    # 일일 재시작: 15:00 UTC = 한국 자정 (미국 동부 오전 10시·서부 오전 7시 — 수면 음악 청취가 가장 적은 때) + 슬롯×2분
    "restart": {"base_utc": "15:00", "slot_minutes": 2},
    "watchdog": {"speed_min": 0.97, "speed_grace_seconds": 60, "stall_seconds": 30,
                 "startup_grace_seconds": 60, "max_restarts_per_hour": 6},
    "collector": {"interval_seconds": 10, "vnstat_interval_seconds": 300},
    # 매시간 정리. 1년을 돌려도 파생 데이터가 수백 MB 를 넘지 않게: 로그 파일당 10MB(넘으면 끝 2MB만),
    # 로그 전체 300MB, DB 200MB, 원본 지표 3일(그 뒤 시간 단위 집계 400일), 이벤트·로그인 기록 30일
    "cleanup": {"interval_minutes": 60, "rejected_days": 7, "incoming_stale_hours": 24, "log_days": 14,
                "metrics_raw_days": 3, "log_max_mb": 10, "log_keep_mb": 2, "logs_total_mb": 300, "db_max_mb": 200,
                "disk_warn_percent": 80, "disk_emergency_percent": 90},
    "capacity": {"monthly_traffic_tb": 30, "port_mbit": 200, "safety_ratio": 0.85, "audio_kbps": 128,
                 "overhead_ratio": 0.03, "cpu_percent_total": 400, "ram_mb_per_live": 300},
    "dashboard": {"username": "admin", "bind": "127.0.0.1", "port": 8080, "session_hours": 168,
                  "login_max_failures": 5, "login_block_minutes": 15, "max_upload_mb": 20000},
    "harbor_port_base": 8101,
    "ui": {"lives_view": "grid"},  # 라이브 화면 보기 방식: grid(블록) | list(목록)
    "server_name": "",  # 예: LIVE 1 — 서버가 여러 대일 때 구분 (화면·알림)
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings(paths: Paths) -> dict:
    data: dict = {}
    if paths.settings_file.exists():
        data = yaml.safe_load(paths.settings_file.read_text(encoding="utf-8")) or {}
    return _merge(DEFAULTS, data)


def save_settings_override(paths: Paths, key_path: list[str], value: Any) -> None:
    """settings.yaml 의 한 항목만 바꾼다 (대시보드 설정 화면용)."""
    data: dict = {}
    if paths.settings_file.exists():
        data = yaml.safe_load(paths.settings_file.read_text(encoding="utf-8")) or {}
    cur = data
    for k in key_path[:-1]:
        cur = cur.setdefault(k, {})
    cur[key_path[-1]] = value
    _atomic_write(paths.settings_file, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------
def server_name(settings: dict) -> str:
    """서버명 (화면 제목·로고 옆·알림 제목). 예전 '알림에 붙일 서버 이름' 값이 있으면 그걸 쓴다."""
    return str(settings.get("server_name") or settings.get("notify", {}).get("server_label") or "").strip()


def parse_bitrate(v: Any) -> int:
    """'5500k' / '5.5M' / 5500 → kbps"""
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)(?:bps|b)?", str(v).strip().lower())
    if not m:
        raise ConfigError(f"비트레이트 형식 오류: {v!r} (예: 5500k)")
    n, u = float(m.group(1)), m.group(2)
    return int(n * 1000) if u == "m" else int(n) if u == "k" else int(n / 1000)


def valid_folder_name(name: str) -> bool:
    """폴더 이름은 Liquidsoap 문자열·ffconcat 에 들어가므로 안전한 문자만. '_' '.' 로 시작하면 시스템 예약."""
    return bool(FOLDER_RE.fullmatch(name or "")) and ".." not in name


def hhmm_to_minutes(s: str) -> int:
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(s).strip())
    if not m:
        raise ConfigError(f"시각 형식 오류: {s!r} (HH:MM)")
    return int(m.group(1)) * 60 + int(m.group(2))


def _atomic_write(path: Path, text: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 라이브
# ---------------------------------------------------------------------------
@dataclass
class Live:
    id: str
    name: str
    enabled: bool = True
    video_folders: list[str] = field(default_factory=list)
    audio_folders: list[str] = field(default_factory=list)
    audio_mode: str = "random_norepeat"
    norepeat_count: int = 30
    crossfade: float = 0.0
    harbor_port: int = 0
    restart_slot: int = 0
    youtube_video_id: str = ""
    output_url: str = ""
    channel_id: str = ""   # 채널 (v2.4)
    key_id: str = ""       # 그 채널에 등록된 스트림 키
    created_at: float = 0.0
    # 타이머: off | duration(방송 시간 뒤 종료) | until(지정 시각에 종료)
    #   유튜브 '자동 중지'를 켜 두면 송출이 끝날 때 방송이 끝나고 다시보기 영상으로 남는다 (12시간 이하만 보관)
    timer_mode: str = "off"
    timer_minutes: int = 710          # 11시간 50분
    timer_until: float = 0.0          # epoch 초
    timer_repeat: bool = False        # 끝나면 timer_gap_minutes 뒤 새 방송으로 다시 시작 (duration 모드)
    timer_gap_minutes: int = 2

    @property
    def number(self) -> int:
        return int(LIVE_ID_RE.match(self.id).group(1))

    def timer_end_at(self, started_at: float | None) -> float | None:
        if self.timer_mode == "duration" and started_at:
            return started_at + int(self.timer_minutes) * 60
        if self.timer_mode == "until" and self.timer_until:
            return float(self.timer_until)
        return None

    def restart_time_utc(self, settings: dict) -> str:
        base = hhmm_to_minutes(settings["restart"]["base_utc"])
        m = (base + self.restart_slot * int(settings["restart"]["slot_minutes"])) % 1440
        return f"{m // 60:02d}:{m % 60:02d}"

    def next_restart(self, settings: dict, now: datetime | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        h, m = map(int, self.restart_time_utc(settings).split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "video_folders": list(self.video_folders), "audio_folders": list(self.audio_folders),
            "audio_mode": self.audio_mode, "norepeat_count": self.norepeat_count, "crossfade": self.crossfade,
            "harbor_port": self.harbor_port, "restart_slot": self.restart_slot,
            "youtube_video_id": self.youtube_video_id, "output_url": self.output_url, "created_at": self.created_at,
            "channel_id": self.channel_id, "key_id": self.key_id,
            "timer_mode": self.timer_mode, "timer_minutes": int(self.timer_minutes), "timer_until": float(self.timer_until),
            "timer_repeat": bool(self.timer_repeat), "timer_gap_minutes": int(self.timer_gap_minutes),
        }

    def validate(self) -> None:
        if not LIVE_ID_RE.match(self.id):
            raise ConfigError(f"ID 형식 오류: {self.id}")
        if not self.name.strip():
            raise ConfigError("이름을 입력하세요")
        if not self.video_folders:
            raise ConfigError("비디오 폴더를 1개 이상 선택하세요")
        if not self.audio_folders:
            raise ConfigError("음악 폴더를 1개 이상 선택하세요")
        for f in [*self.video_folders, *self.audio_folders]:
            if not valid_folder_name(f):
                raise ConfigError(f"폴더 이름 사용 불가: {f}")
        if self.audio_mode not in AUDIO_MODES:
            raise ConfigError(f"재생 방식 오류: {self.audio_mode}")
        if not (0 <= int(self.norepeat_count) <= 1000):
            raise ConfigError("중복방지 곡 수는 0~1000")
        if not (0 <= float(self.crossfade) <= 10):
            raise ConfigError("크로스페이드는 0~10초")
        if not (1024 < int(self.harbor_port) < 65536):
            raise ConfigError("오디오 포트 오류")
        if self.timer_mode not in ("off", "duration", "until"):
            raise ConfigError("타이머 방식 오류")
        if self.timer_mode == "duration" and not (10 <= int(self.timer_minutes) <= 24 * 60):
            raise ConfigError("방송 시간은 10분 ~ 24시간")
        if self.timer_mode == "until" and float(self.timer_until) <= 0:
            raise ConfigError("종료 날짜·시각을 입력하세요")
        if not (1 <= int(self.timer_gap_minutes) <= 120):
            raise ConfigError("다시 시작 간격은 1~120분")
        if bool(self.channel_id) != bool(self.key_id):
            raise ConfigError("채널과 스트림 키를 함께 고르세요")
        if self.youtube_video_id and not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", self.youtube_video_id):
            raise ConfigError("유튜브 영상 ID 형식 오류")


def live_from_dict(d: dict) -> Live:
    known = Live.__dataclass_fields__.keys()
    lv = Live(**{k: v for k, v in (d or {}).items() if k in known})
    lv.video_folders = [str(x) for x in (lv.video_folders or [])]
    lv.audio_folders = [str(x) for x in (lv.audio_folders or [])]
    lv.crossfade = float(lv.crossfade or 0)
    lv.norepeat_count = int(lv.norepeat_count)
    lv.timer_minutes, lv.timer_gap_minutes = int(lv.timer_minutes), int(lv.timer_gap_minutes)
    lv.timer_until, lv.timer_repeat = float(lv.timer_until or 0), bool(lv.timer_repeat)
    return lv


class LiveStore:
    """data/config/lives/<id>.yaml 읽기·쓰기 + 자동 값 배정."""

    def __init__(self, paths: Paths, settings: dict):
        self.paths = paths
        self.settings = settings

    def _file(self, live_id: str) -> Path:
        return self.paths.lives_dir / f"{live_id}.yaml"

    def all(self) -> list[Live]:
        out = []
        if self.paths.lives_dir.is_dir():
            for p in self.paths.lives_dir.glob("live*.yaml"):
                try:
                    out.append(live_from_dict(yaml.safe_load(p.read_text(encoding="utf-8"))))
                except Exception:  # noqa: BLE001 — 깨진 파일 하나가 전체를 막지 않게
                    continue
        return sorted(out, key=lambda lv: lv.number)

    def get(self, live_id: str) -> Live | None:
        if not LIVE_ID_RE.match(live_id or ""):
            return None
        p = self._file(live_id)
        if not p.exists():
            return None
        return live_from_dict(yaml.safe_load(p.read_text(encoding="utf-8")))

    def new(self, name: str) -> Live:
        lives = self.all()
        used_n = {lv.number for lv in lives}
        n = next(i for i in range(1, 10000) if i not in used_n)
        used_ports = {lv.harbor_port for lv in lives}
        base = int(self.settings["harbor_port_base"])
        port = next(p for p in range(base, base + 5000) if p not in used_ports and p != int(self.settings["dashboard"]["port"]))
        used_slots = {lv.restart_slot for lv in lives}
        slot = next(s for s in range(0, 720) if s not in used_slots)
        return Live(id=f"live{n}", name=name, harbor_port=port, restart_slot=slot, created_at=time.time())

    def save(self, lv: Live) -> None:
        lv.validate()
        for other in self.all():
            if other.id == lv.id:
                continue
            if other.harbor_port == lv.harbor_port:
                raise ConfigError("오디오 포트 중복")
            if other.restart_slot == lv.restart_slot:
                raise ConfigError("재시작 슬롯 중복")
            if lv.key_id and (other.channel_id, other.key_id) == (lv.channel_id, lv.key_id):
                raise ConfigError(f"이 스트림 키는 '{other.name}' 라이브에 이미 등록되어 있습니다")
        _atomic_write(self._file(lv.id), yaml.safe_dump(lv.to_dict(), allow_unicode=True, sort_keys=False))

    def delete(self, live_id: str) -> None:
        self._file(live_id).unlink(missing_ok=True)
        self.paths.stream_key(live_id).unlink(missing_ok=True)

    def folder_usage(self, kind: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for lv in self.all():
            for f in (lv.video_folders if kind == "video" else lv.audio_folders):
                out.setdefault(f, []).append(lv.id)
        return out
