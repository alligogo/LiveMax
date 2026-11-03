"""대시보드 인증 (DESIGN 9.1, 9.2).

- 비밀번호는 argon2 해시로만 저장 (secrets/admin.hash). 평문 저장 금지.
- 초기 비밀번호는 설치 시 랜덤 생성 → secrets/admin.initial_password(600) + 콘솔 출력.
  첫 로그인 후 변경을 강제한다 (secrets/admin.must_change 표시 파일).
- 로그인 5회 실패 시 해당 IP 15분 차단.
- 세션: 랜덤 토큰을 쿠키(HttpOnly, Secure, SameSite=Strict)로, DB 에는 SHA-256 해시만 저장.
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets
import sqlite3
import string
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .. import paths as _paths
from ..secrets import write_secret

_ph = PasswordHasher()
COOKIE = "lm_session"
MIN_PASSWORD_LEN = 12


def hash_file() -> Path:
    return _paths.SECRETS_DIR / "admin.hash"


def must_change_file() -> Path:
    return _paths.SECRETS_DIR / "admin.must_change"


def initial_file() -> Path:
    return _paths.SECRETS_DIR / "admin.initial_password"


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(pysecrets.choice(alphabet) for _ in range(length))


def set_password(pw: str, must_change: bool = False) -> None:
    write_secret(hash_file(), _ph.hash(pw))
    if must_change:
        write_secret(must_change_file(), "1")
    else:
        must_change_file().unlink(missing_ok=True)
        initial_file().unlink(missing_ok=True)  # 초기 비밀번호 파일은 변경 후 즉시 삭제


def reset_to_random() -> str:
    """랜덤 비밀번호로 재설정하고 평문을 돌려준다 (설치·분실 시)."""
    pw = generate_password()
    set_password(pw, must_change=True)
    write_secret(initial_file(), pw)
    return pw


def verify_password(pw: str) -> bool:
    try:
        stored = hash_file().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    try:
        ok = _ph.verify(stored, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    if ok and _ph.check_needs_rehash(stored):
        write_secret(hash_file(), _ph.hash(pw))
    return ok


def must_change() -> bool:
    return must_change_file().exists()


def password_problems(pw: str, username: str) -> list[str]:
    p = []
    if len(pw) < MIN_PASSWORD_LEN:
        p.append(f"{MIN_PASSWORD_LEN}자 이상이어야 합니다")
    if pw.lower() in (username.lower(), "password", "000000000000"):
        p.append("너무 쉬운 비밀번호입니다")
    if len(set(pw)) < 5:
        p.append("서로 다른 문자를 더 섞어 주세요")
    return p


# ---------------- 로그인 시도 / 차단 ----------------

def blocked_seconds(conn: sqlite3.Connection, ip: str, max_failures: int, block_minutes: int) -> int:
    """최근 block_minutes 안에서 마지막 성공 이후 실패가 max_failures 이상이면 남은 차단 시간(초)."""
    window = time.time() - block_minutes * 60
    rows = conn.execute(
        "SELECT ts, success FROM login_attempts WHERE ip=? AND ts>=? ORDER BY ts DESC", (ip, window)
    ).fetchall()
    fails = []
    for r in rows:
        if r["success"]:
            break
        fails.append(r["ts"])
    if len(fails) >= max_failures:
        # 차단은 max_failures 번째 실패 시각부터 block_minutes
        trigger = sorted(fails)[-max_failures] if len(fails) >= max_failures else fails[0]
        remain = int(trigger + block_minutes * 60 - time.time())
        return max(remain, 0)
    return 0


def record_attempt(conn: sqlite3.Connection, ip: str, username: str, success: bool) -> None:
    conn.execute("INSERT INTO login_attempts(ts, ip, username, success) VALUES (?,?,?,?)",
                 (time.time(), ip, username[:64], 1 if success else 0))


# ---------------- 세션 ----------------

def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, ip: str, ua: str, hours: float) -> str:
    token = pysecrets.token_urlsafe(32)
    now = time.time()
    conn.execute("INSERT INTO sessions(token_hash, created, last_seen, expires, ip, user_agent) VALUES (?,?,?,?,?,?)",
                 (_h(token), now, now, now + hours * 3600, ip, ua[:200]))
    return token


def get_session(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (_h(token),)).fetchone()
    if not row:
        return None
    now = time.time()
    if row["expires"] < now:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
        return None
    if now - row["last_seen"] > 60:
        conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, row["token_hash"]))
    return row


def token_hash(token: str) -> str:
    return _h(token)


def revoke(conn: sqlite3.Connection, token_hash_value: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash_value,))


def revoke_all_except(conn: sqlite3.Connection, keep_hash: str | None) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash != ?", (keep_hash or "",))
