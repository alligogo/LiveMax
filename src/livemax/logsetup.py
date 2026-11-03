"""로깅 설정 (DESIGN 6.3).

- 처음에는 과할 만큼 상세하게 (기본 DEBUG).
- 파일은 /var/log/livemax/<component>.log. 로테이션은 logrotate(deploy/logrotate/livemax)가 담당하므로
  WatchedFileHandler 를 쓴다 (logrotate 가 파일을 옮기면 자동으로 새 파일을 연다).
- 모든 핸들러에 Redactor 를 붙여 스트림 키가 로그에 평문으로 남지 않게 한다.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path

from .secrets import Redactor


class UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):  # noqa: N802
        t = time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created))
        return f"{t}.{int(record.msecs):03d}Z"


FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def setup(component: str, server: dict | None = None, to_stderr: bool = True) -> logging.Logger:
    level_name = "DEBUG"
    log_dir: Path | None = None
    if server:
        level_name = str(server.get("logging", {}).get("level", "DEBUG")).upper()
        log_dir = Path(server["paths"]["log_dir"])

    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.DEBUG))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = UTCFormatter(FMT)
    red = Redactor()

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.WatchedFileHandler(log_dir / f"{component}.log", encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(red)
            root.addHandler(fh)
        except OSError as e:
            print(f"[logsetup] 로그 파일을 열 수 없음 ({e}); stderr 로만 기록", file=sys.stderr)

    if to_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.addFilter(red)
        root.addHandler(sh)

    # 서드파티 라이브러리 소음 억제
    for noisy in ("multipart", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(f"livemax.{component}")
