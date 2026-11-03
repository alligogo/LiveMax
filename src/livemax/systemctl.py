"""systemd 제어 래퍼.

서비스들은 livemax 계정으로 돌기 때문에 sudo -n 으로 허용된 명령만 쓴다.
허용 범위는 deploy/sudoers/livemax 에서 live@*, live-audio@*, livemax-cleanup 으로 제한된다.
라이브 1개를 다룰 때 다른 라이브 유닛은 절대 건드리지 않는다 (DESIGN 11-8).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("livemax.systemctl")
# sudoers 규칙이 정확한 경로를 매칭하므로 고정 경로를 우선한다
SYSTEMCTL = "/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else (shutil.which("systemctl") or "systemctl")
_UNIT_RE = re.compile(r"^(live@|live-audio@)[a-z0-9][a-z0-9_-]{0,31}\.service$|^livemax-[a-z-]+\.service$")


def video_unit(live_id: str) -> str:
    return f"live@{live_id}.service"


def audio_unit(live_id: str) -> str:
    return f"live-audio@{live_id}.service"


def _cmd(*args: str) -> list[str]:
    base = [SYSTEMCTL, *args]
    if os.geteuid() != 0:
        base = ["sudo", "-n", *base]
    return base


def _check_unit(unit: str) -> None:
    if not _UNIT_RE.match(unit):
        raise ValueError(f"허용되지 않은 유닛: {unit}")


def action(verb: str, unit: str, timeout: int = 60) -> bool:
    if verb not in ("start", "stop", "restart", "enable", "disable"):
        raise ValueError(verb)
    _check_unit(unit)
    log.info("systemctl %s %s", verb, unit)
    try:
        r = subprocess.run(_cmd(verb, "--no-block", unit) if verb in ("start", "restart") else _cmd(verb, unit),
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("systemctl %s %s 실패: %s", verb, unit, e)
        return False
    if r.returncode != 0:
        log.error("systemctl %s %s rc=%d: %s", verb, unit, r.returncode, r.stderr.strip())
        return False
    return True


def restart(unit: str) -> bool:
    return action("restart", unit)


def is_active(unit: str) -> str:
    """'active' / 'inactive' / 'failed' / 'activating' / 'unknown'"""
    try:
        r = subprocess.run([SYSTEMCTL, "is-active", unit], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def main_pid(unit: str) -> int | None:
    try:
        r = subprocess.run([SYSTEMCTL, "show", "-p", "MainPID", "--value", unit], capture_output=True, text=True, timeout=10)
        pid = int(r.stdout.strip() or 0)
        return pid or None
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None
