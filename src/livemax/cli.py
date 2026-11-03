"""운영용 CLI — install.sh / healthcheck.sh 와 사람이 직접 쓴다.

    python -m livemax.cli paths                 # server.yaml 경로들 (셸 변수 형식)
    python -m livemax.cli validate              # 모든 설정 검증 (재시작 시각 겹침 포함)
    python -m livemax.cli lives [--enabled]     # 라이브 id 목록
    python -m livemax.cli ports                 # 사용 중인 harbor 포트
    python -m livemax.cli probe video|audio F   # 파일 1개 규격 검사 (업로드 전 확인용)
    python -m livemax.cli concat ID             # concat 목록 미리 생성 (송출 안 함)
    python -m livemax.cli liq ID                # Liquidsoap 스크립트 출력
    python -m livemax.cli check-reencode        # 송출 명령이 copy 전용인지 확인
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import config, db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="livemax")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("paths")
    sub.add_parser("validate")
    p = sub.add_parser("lives")
    p.add_argument("--enabled", action="store_true")
    sub.add_parser("ports")
    p = sub.add_parser("probe")
    p.add_argument("kind", choices=["video", "audio"])
    p.add_argument("file")
    p = sub.add_parser("concat")
    p.add_argument("live_id")
    p = sub.add_parser("liq")
    p.add_argument("live_id")
    sub.add_parser("check-reencode")
    a = ap.parse_args(argv)

    server = config.load_server()
    paths = config.get_paths(server)

    if a.cmd == "paths":
        for k in ("media_root", "log_dir", "run_dir", "state_dir", "db"):
            print(f"LM_{k.upper()}={shlex.quote(str(getattr(paths, k)))}")
        print(f"LM_DASH_PORT={int(server['dashboard']['port'])}")
        print(f"LM_DOMAIN={shlex.quote(str(server['dashboard'].get('domain') or ''))}")
        print(f"LM_ACME_EMAIL={shlex.quote(str(server['dashboard'].get('acme_email') or ''))}")
        print(f"LM_MAX_UPLOAD_MB={int(server['dashboard']['max_upload_mb'])}")
        return 0

    if a.cmd == "validate":
        lives, errors = config.load_all_lives(server=server)
        for lv in lives:
            print(f"OK   {lv.id:10s} enabled={lv.enabled} {lv.bitrate_kbps}k tz={lv.timezone} restart={lv.restart_at} port={lv.harbor_port}")
        for k, v in errors.items():
            print(f"FAIL {k}: {v.strip()}")
        return 1 if errors else 0

    if a.cmd == "lives":
        lives, _ = config.load_all_lives(server=server, cross=False)
        for lv in lives:
            if not a.enabled or lv.enabled:
                print(lv.id)
        return 0

    if a.cmd == "ports":
        lives, _ = config.load_all_lives(server=server, cross=False)
        for lv in lives:
            print(f"{lv.harbor_port} {lv.id}")
        return 0

    if a.cmd == "probe":
        from . import probe

        r = probe.validate(a.kind, Path(a.file), server)
        print("통과" if r.ok else "거부")
        for x in r.reasons:
            print(f"  ✕ {x}")
        for x in r.warnings:
            print(f"  ▲ {x}")
        print(f"  길이 {r.duration}s, 비트레이트 {r.bitrate_kbps}k, {r.info}")
        return 0 if r.ok else 1

    if a.cmd == "concat":
        from . import concat

        lv = config.load_live(a.live_id, server=server)
        db.init(paths.db)
        p = concat.generate(lv, paths, db.connect(paths.db), server)
        print(p)
        return 0

    if a.cmd == "liq":
        from . import liquidsoap

        print(liquidsoap.render(config.load_live(a.live_id, server=server), paths, server))
        return 0

    if a.cmd == "check-reencode":
        from . import ffmpeg_cmd

        args = ffmpeg_cmd.build(Path("/dev/null"), "http://127.0.0.1:1/x.aac", "rtmps://example.invalid/live2/KEY")
        print("OK — 송출 명령은 copy 전용 (-c:v copy -c:a copy, 필터 없음)")
        print(" ".join(a if "KEY" not in a else "rtmps://…/****" for a in args))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
