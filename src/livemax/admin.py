"""관리 명령 (컨테이너 안에서): python3 -m livemax.admin reset-password

호스트에서는 `sudo livemax password` 로 부른다.
"""

from __future__ import annotations

import sys

from .web import auth


def main(argv: list[str]) -> int:
    if argv[:1] == ["reset-password"]:
        pw = auth.reset_to_random()
        print("=" * 52)
        print(" 대시보드 관리자 비밀번호를 재설정했습니다")
        print("   아이디:   admin")
        print(f"   비밀번호: {pw}")
        print("   (다음 로그인 때 새 비밀번호로 바꾸게 됩니다)")
        print("=" * 52)
        return 0
    print("사용법: python3 -m livemax.admin reset-password")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
