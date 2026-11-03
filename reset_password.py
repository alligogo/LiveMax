#!/usr/bin/env python3
"""대시보드 관리자 비밀번호 재설정 (DESIGN 9.2).

    sudo -u livemax /opt/livemax/venv/bin/python /opt/livemax/reset_password.py

랜덤 비밀번호를 생성해 argon2 해시로 저장하고, 평문은 콘솔과 secrets/admin.initial_password(600)에
남긴다. 다음 로그인 때 변경이 강제되며, 변경하면 초기 비밀번호 파일은 삭제된다.
고정 초기값(0000 등)은 절대 쓰지 않는다.

  --if-missing : 이미 비밀번호가 설정되어 있으면 아무것도 하지 않음 (install.sh 멱등성용)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent
sys.path.insert(0, str(HOME / "src"))
os.environ.setdefault("LIVEMAX_HOME", str(HOME))

from livemax.web import auth  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--if-missing", action="store_true")
    a = ap.parse_args()
    if a.if_missing and auth.hash_file().exists():
        print("관리자 비밀번호가 이미 설정되어 있음 — 건너뜀")
        return 0
    pw = auth.reset_to_random()
    print("=" * 56)
    print(" LiveMax 대시보드 관리자 비밀번호 (첫 로그인 후 변경 필수)")
    print("   아이디:   (config/server.yaml dashboard.username, 기본 admin)")
    print(f"   비밀번호: {pw}")
    print(f"   저장 위치: {auth.initial_file()} (권한 600, 변경 시 자동 삭제)")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
