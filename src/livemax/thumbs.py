"""영상 썸네일 — 업로드 시 1회만 생성 (DESIGN 8.4, 11-9).

송출 중인 스트림에서는 절대 캡처하지 않는다. 업로드된 클립 파일에서 키프레임 1장만 디코딩해
가로 480px JPEG(약 15~30KB)로 저장한다. 송출 경로와 무관한 1회성 작업이다 (DESIGN 11-1 은 송출 재인코딩 금지).
v2.2 까지는 원본 해상도로 저장해 1080p 는 장당 ~300KB, 4K 는 ~1MB 였고 관리 화면이 느렸다.
썸네일은 미디어 원본 폴더가 아니라 state/thumbs 에 저장한다 (원본 폴더에 파생물 금지).
원본 파일을 지우면 썸네일도 같이 지워지고, 고아 썸네일은 정리 작업이 매시간 지운다.
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


WIDTH, HEIGHT = 480, 270  # 16:9 (허용 해상도는 모두 16:9)
MAX_BYTES = 60_000          # 이보다 크면 예전 원본 크기 썸네일 → 다시 만든다


def make_thumbnail(src: Path, dst: Path, at_seconds: float = 1.0) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.jpg")
    args = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1",
        "-skip_frame", "nokey", "-ss", f"{at_seconds}", "-i", str(src),  # 키프레임만 디코딩 (4K 도 빠름)
        "-frames:v", "1", "-s", f"{WIDTH}x{HEIGHT}", "-q:v", "5",
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


def shrink_existing(paths: Paths) -> int:
    """예전(원본 크기) 썸네일을 작은 것으로 다시 만든다. 시작할 때 백그라운드에서 1회."""
    base = paths.thumbs_dir / "video"
    n = 0
    if not base.is_dir():
        return 0
    for t in base.rglob("*.jpg"):
        try:
            if t.stat().st_size <= MAX_BYTES:
                continue
        except OSError:
            continue
        src = paths.video / t.parent.name / t.name[:-4]
        if src.is_file() and make_thumbnail(src, t):
            n += 1
    if n:
        log.info("썸네일 %d개를 %dx%d 로 줄였습니다", n, WIDTH, HEIGHT)
    return n
