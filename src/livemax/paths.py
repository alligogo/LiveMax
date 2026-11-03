"""데이터 경로 (v2).

컨테이너 안에서는 모든 데이터가 /data 한 곳에 있다 (호스트 /opt/livemax/data 를 마운트).
코드(이미지)와 데이터(볼륨)를 분리하므로 업데이트 = 이미지 교체, 데이터는 그대로 남는다.

/data
  config/settings.yaml      서버 설정 (선택 — 없으면 기본값)
  config/lives/<id>.yaml    라이브 설정 (대시보드가 관리)
  secrets/                  스트림 키, 관리자 비밀번호 해시 (700)
  media/_incoming/<kind>/<폴더>/   업로드 스테이징 (SFTP 도 여기로)
  media/_rejected/          검증 실패 격리
  media/video/<폴더>/, media/audio/<폴더>/   방송 원본
  state/livemax.db          지표·이벤트·카탈로그·세션
  state/concat/, state/thumbs/, state/uploads/, state/caddy/
  logs/                     로그
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = Path(os.environ.get("LIVEMAX_ASSETS", CODE_ROOT / "assets"))


def data_dir() -> Path:
    return Path(os.environ.get("LIVEMAX_DATA", "/data"))


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config(self) -> Path: return self.root / "config"
    @property
    def lives_dir(self) -> Path: return self.config / "lives"
    @property
    def settings_file(self) -> Path: return self.config / "settings.yaml"
    @property
    def secrets(self) -> Path: return self.root / "secrets"
    @property
    def media(self) -> Path: return self.root / "media"
    @property
    def incoming(self) -> Path: return self.media / "_incoming"
    @property
    def rejected(self) -> Path: return self.media / "_rejected"
    @property
    def video(self) -> Path: return self.media / "video"
    @property
    def audio(self) -> Path: return self.media / "audio"
    @property
    def state(self) -> Path: return self.root / "state"
    @property
    def db(self) -> Path: return self.state / "livemax.db"
    @property
    def concat_dir(self) -> Path: return self.state / "concat"
    @property
    def thumbs_dir(self) -> Path: return self.state / "thumbs"
    @property
    def uploads(self) -> Path: return self.state / "uploads"
    @property
    def run_dir(self) -> Path: return self.state / "run"
    @property
    def logs(self) -> Path: return self.root / "logs"

    def kind_root(self, kind: str) -> Path:
        if kind == "video":
            return self.video
        if kind == "audio":
            return self.audio
        raise ValueError(kind)

    def stream_key(self, live_id: str) -> Path:
        return self.secrets / f"{live_id}.key"

    @property
    def protected_roots(self) -> tuple[Path, ...]:
        """자동 정리가 절대 건드리면 안 되는 미디어 원본 (DESIGN 11-11)."""
        return (self.video, self.audio)

    def ensure(self) -> None:
        for d in (self.lives_dir, self.media, self.incoming / "video", self.incoming / "audio", self.rejected,
                  self.video, self.audio, self.concat_dir, self.thumbs_dir, self.uploads, self.run_dir, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.secrets, 0o700)


def get_paths() -> Paths:
    return Paths(data_dir())


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
