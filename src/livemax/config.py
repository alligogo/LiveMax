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
        "video": {"codec": "h264", "profile": "High", "width": 1920, "height": 1080, "fps": 30,
                  "min_bitrate": "5000k", "keyframe_interval": 2.0, "keyframe_tolerance": 0.05,
                  "containers": ["mp4", "mkv"]},
        "audio": {"codec": "aac", "profile": "LC", "bitrate": "128k", "bitrate_tolerance": 0.10,
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
        "restart_backoff_seconds": [2, 5, 10, 30, 60],
    },
    "restart": {"base_utc": "09:00", "slot_minutes": 2},  # 일일 재시작: 09:00 UTC(미국 새벽) + 슬롯×2분
    "watchdog": {"speed_min": 0.97, "speed_grace_seconds": 60, "stall_seconds": 30,
                 "startup_grace_seconds": 60, "max_restarts_per_hour": 6},
    "collector": {"interval_seconds": 10, "vnstat_interval_seconds": 300},
    "cleanup": {"time_utc": "08:41", "rejected_days": 7, "incoming_stale_hours": 24, "log_days": 14,
                "metrics_raw_days": 30, "disk_warn_percent": 80, "disk_emergency_percent": 90},
    "capacity": {"monthly_traffic_tb": 32, "port_mbit": 200, "safety_ratio": 0.85, "audio_kbps": 128,
                 "overhead_ratio": 0.03, "cpu_percent_total": 400, "ram_mb_per_live": 300},
    "dashboard": {"username": "admin", "bind": "127.0.0.1", "port": 8080, "session_hours": 168,
                  "login_max_failures": 5, "login_block_minutes": 15, "max_upload_mb": 20000},
    "harbor_port_base": 8101,
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
    bitrate: str = "5500k"
    harbor_port: int = 0
    restart_slot: int = 0
    youtube_video_id: str = ""
    output_url: str = ""
    created_at: float = 0.0

    @property
    def number(self) -> int:
        return int(LIVE_ID_RE.match(self.id).group(1))

    @property
    def bitrate_kbps(self) -> int:
        return parse_bitrate(self.bitrate)

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
            "bitrate": self.bitrate, "harbor_port": self.harbor_port, "restart_slot": self.restart_slot,
            "youtube_video_id": self.youtube_video_id, "output_url": self.output_url, "created_at": self.created_at,
        }

    def validate(self) -> None:
        if not LIVE_ID_RE.match(self.id):
            raise ConfigError(f"ID 형식 오류: {self.id}")
        if not self.name.strip():
            raise ConfigError("이름을 입력하세요")
        if not self.video_folders:
            raise ConfigError("영상 폴더를 1개 이상 선택하세요")
        if not self.audio_folders:
            raise ConfigError("음원 폴더를 1개 이상 선택하세요")
        for f in [*self.video_folders, *self.audio_folders]:
            if not valid_folder_name(f):
                raise ConfigError(f"폴더 이름 사용 불가: {f}")
        if self.audio_mode not in AUDIO_MODES:
            raise ConfigError(f"재생 방식 오류: {self.audio_mode}")
        if not (0 <= int(self.norepeat_count) <= 1000):
            raise ConfigError("중복방지 곡 수는 0~1000")
        if not (0 <= float(self.crossfade) <= 10):
            raise ConfigError("크로스페이드는 0~10초")
        if not (1000 <= self.bitrate_kbps <= 51000):
            raise ConfigError(f"비트레이트 범위 오류: {self.bitrate}")
        if not (1024 < int(self.harbor_port) < 65536):
            raise ConfigError("오디오 포트 오류")
        if self.youtube_video_id and not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", self.youtube_video_id):
            raise ConfigError("유튜브 영상 ID 형식 오류")


def live_from_dict(d: dict) -> Live:
    known = Live.__dataclass_fields__.keys()
    lv = Live(**{k: v for k, v in (d or {}).items() if k in known})
    lv.video_folders = [str(x) for x in (lv.video_folders or [])]
    lv.audio_folders = [str(x) for x in (lv.audio_folders or [])]
    lv.crossfade = float(lv.crossfade or 0)
    lv.norepeat_count = int(lv.norepeat_count)
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
