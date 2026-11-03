"""라이브 1개의 오디오 엔진 — live-audio@<id>.service

라이브 YAML 에서 Liquidsoap 스크립트를 생성하고, TZ=<라이브 timezone> 환경으로 liquidsoap 을 exec 한다.
exec 이므로 PID 가 그대로 유지되어 systemd 가 liquidsoap 을 직접 감시한다.

Liquidsoap 은 영상 쪽 ffmpeg(live@<id>)와 독립적으로 계속 돈다. ffmpeg 가 일일 재시작돼도
음악 편성(중복방지 기록 포함)은 끊기지 않는다.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from . import config, liquidsoap, logsetup

LIQUIDSOAP = shutil.which("liquidsoap") or "liquidsoap"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LiveMax 오디오 (Liquidsoap)")
    ap.add_argument("live_id")
    ap.add_argument("--render-only", action="store_true", help=".liq 파일만 생성하고 경로 출력")
    ap.add_argument("--check", action="store_true", help="생성 후 liquidsoap --check 로 문법 검사")
    a = ap.parse_args(argv)

    server = config.load_server()
    paths = config.get_paths(server)
    log = logsetup.setup(f"audio-launcher-{a.live_id}", server)

    try:
        live = config.load_live(a.live_id, server=server)
    except config.ConfigError as e:
        log.error("설정 오류: %s", e)
        return 1
    if not live.enabled and not (a.render_only or a.check):
        log.warning("%s enabled: false — 오디오 시작 안 함", live.id)
        return 78

    for folder in sorted(live.audio_folders()):
        if not (paths.audio / folder).is_dir():
            log.warning("음원 폴더 없음: %s (무음으로 송출됨)", paths.audio / folder)

    liq = liquidsoap.write(live, paths, server)
    log.info("Liquidsoap 스크립트 생성: %s (mode=%s, norepeat=%d, tz=%s, port=%d)",
             liq, live.audio_mode, live.norepeat_count, live.timezone, live.harbor_port)

    env = dict(os.environ)
    env["TZ"] = live.timezone  # 지역명 — 서머타임은 tzdata 가 계산 (오프셋 하드코딩 금지)

    if a.render_only:
        print(liq)
        return 0
    if a.check:
        r = subprocess.run([LIQUIDSOAP, "--check", str(liq)], env=env)
        return r.returncode

    paths.audio_pid_file(live.id).write_text(str(os.getpid()))  # exec 후에도 같은 PID
    os.execvpe(LIQUIDSOAP, [LIQUIDSOAP, str(liq)], env)
    return 0  # 도달하지 않음


if __name__ == "__main__":
    sys.exit(main())
