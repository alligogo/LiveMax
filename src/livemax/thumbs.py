"""영상 썸네일 — 업로드 시 1회만 생성 (DESIGN 8.4, 11-9).

송출 중인 스트림에서는 절대 캡처하지 않는다. 업로드된 클립 파일에서 프레임 1장만 디코딩한다.
스케일 필터(-vf scale)도 쓰지 않는다: 원본 해상도 JPEG 1장 (~200KB). 표시 크기는 CSS 가 줄인다.
썸네일은 미디어 원본 폴더가 아니라 state_dir/thumbs 에 저장한다 (원본 폴더에 파생물 금지).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .paths import Paths

log = logging.getLogger("livemax.thumbs")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def thumb_path(paths: Paths, folder: str, name: str) -> Path:
    return paths.thumbs_dir / "video" / folder / f"{name}.jpg"


def make_thumbnail(src: Path, dst: Path, at_seconds: float = 1.0) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.jpg")
    args = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{at_seconds}", "-i", str(src),
        "-frames:v", "1", "-q:v", "4",  # 필터 없음
        str(tmp),
    ]
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=60)
        tmp.replace(dst)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("썸네일 생성 실패 %s: %s", src.name, getattr(e, "stderr", b"")[-300:])
        tmp.unlink(missing_ok=True)
        return False
