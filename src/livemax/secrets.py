"""비밀 정보 취급 (DESIGN 5.2, 9.5).

- 스트림 키는 secrets/ 에만, 파일 권한 600.
- 화면·로그·에러 메시지 어디에도 평문으로 출력하지 않는다.
  → 읽어 들인 비밀값은 전부 Redactor 에 등록되어 로그에서 자동 마스킹된다.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

_registered: set[str] = set()


def mask(value: str | None) -> str:
    """'abcd-efgh-1a2b' → '****1a2b'. 짧은 값은 전부 가린다."""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 8:
        return "****"
    return "****" + v[-4:]


def register_secret(value: str) -> None:
    if value and len(value) >= 6:
        _registered.add(value)


def redact(text: str) -> str:
    for s in _registered:
        if s in text:
            text = text.replace(s, mask(s))
    return text


class Redactor(logging.Filter):
    """모든 로그 레코드에서 등록된 비밀값을 마스킹한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _registered:
            msg = record.getMessage()
            red = redact(msg)
            if red != msg:
                record.msg = red
                record.args = ()
        return True


def check_perms(path: Path) -> str | None:
    """권한이 600(또는 400)보다 넓으면 경고 문자열을 돌려준다."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return f"{path.name}: 파일 없음"
    if mode & 0o077:
        return f"{path.name}: 권한 {oct(mode)} — 600 이어야 함"
    return None


def read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    register_secret(value)
    return value


def read_stream_key(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"스트림 키 파일 없음: {path}")
    warn = check_perms(path)
    if warn:
        logging.getLogger("livemax.secrets").warning("스트림 키 권한 경고: %s", warn)
    key = read_secret(path)
    if not key:
        raise ValueError(f"스트림 키 파일이 비어 있음: {path}")
    return key


def write_secret(path: Path, value: str) -> None:
    """원자적으로 600 권한 파일 쓰기."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value.strip() + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    register_secret(value.strip())
