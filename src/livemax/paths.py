"""경로 해석.

코드(/opt/livemax)와 데이터(config/, secrets/, /media, 로그, DB)를 분리한다 (DESIGN 10.2).
경로는 전부 server.yaml 에서 오며, 코드에는 기본값만 있다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 코드 루트. systemd 유닛에서 LIVEMAX_HOME 으로 지정한다. 없으면 이 파일 기준으로 추정.
HOME = Path(os.environ.get("LIVEMAX_HOME", Path(__file__).resolve().parents[2]))

# 테스트·개발용으로 config/secrets 위치를 바꿀 수 있게 환경변수를 허용한다.
CONFIG_DIR = Path(os.environ.get("LIVEMAX_CONFIG_DIR", HOME / "config"))
SECRETS_DIR = Path(os.environ.get("LIVEMAX_SECRETS_DIR", HOME / "secrets"))
ASSETS_DIR = HOME / "assets"
CONFIG_EXAMPLE_DIR = HOME / "config.example"


@dataclass(frozen=True)
class Paths:
    media_root: Path
    log_dir: Path
    run_dir: Path
    state_dir: Path
    db: Path

    # --- 미디어 ---
    @property
    def incoming(self) -> Path:
        """업로드 스테이징. 구조: _incoming/<video|audio>/<폴더>/<파일>"""
        return self.media_root / "_incoming"

    @property
    def incoming_tmp(self) -> Path:
        """웹 업로드가 전송 중일 때 쓰는 임시 위치 (*.part)."""
        return self.incoming / ".tmp"

    @property
    def rejected(self) -> Path:
        return self.media_root / "_rejected"

    @property
    def video(self) -> Path:
        return self.media_root / "video"

    @property
    def audio(self) -> Path:
        return self.media_root / "audio"

    def kind_root(self, kind: str) -> Path:
        if kind == "video":
            return self.video
        if kind == "audio":
            return self.audio
        raise ValueError(f"unknown media kind: {kind}")

    @property
    def protected_roots(self) -> tuple[Path, ...]:
        """자동 정리가 절대 건드리면 안 되는 미디어 원본 위치 (DESIGN 6.4, 11-11)."""
        return (self.video, self.audio)

    # --- 파생물 ---
    @property
    def concat_dir(self) -> Path:
        return self.state_dir / "concat"

    @property
    def thumbs_dir(self) -> Path:
        return self.state_dir / "thumbs"

    # --- 런타임 파일 (/run/livemax) ---
    def status_file(self, live_id: str) -> Path:
        return self.run_dir / f"{live_id}.status.json"

    def nowplaying_file(self, live_id: str) -> Path:
        return self.run_dir / f"{live_id}.nowplaying.json"

    def liq_file(self, live_id: str) -> Path:
        return self.run_dir / f"{live_id}.liq"

    def audio_pid_file(self, live_id: str) -> Path:
        return self.run_dir / f"{live_id}.audio.pid"


def from_server_config(server: dict) -> Paths:
    p = server["paths"]
    return Paths(
        media_root=Path(p["media_root"]),
        log_dir=Path(p["log_dir"]),
        run_dir=Path(p["run_dir"]),
        state_dir=Path(p["state_dir"]),
        db=Path(p["db"]),
    )


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
