"""파일 이름 검사 — 웹 업로드 · 구글 링크 · WinSCP(_incoming) 세 길 모두 같은 규칙 (DESIGN 11-D 5).

한글·일본어 등 모든 언어 이름은 허용한다. 막는 것은 송출 프로그램(ffconcat·Liquidsoap 재생목록)이나
리눅스 파일 시스템에서 문제를 일으키는 것만:
  - 점(.)으로 시작 — 숨김 파일 (올리는 중인 임시 파일과 구분이 안 됨)
  - '#{' — Liquidsoap 이 문자열 안에서 특수 기호로 해석
  - 줄바꿈·탭 등 보이지 않는 제어 문자 — 재생목록 한 줄이 두 줄로 쪼개짐
  - 허용 확장자 밖
  - UTF-8 240바이트 초과 (리눅스 한도 255 — 썸네일 등 덧붙는 이름 여유). 한글은 글자당 3바이트라 약 80자
맥에서 올린 한글 이름(자모가 풀린 NFD)은 NFC 로 합쳐 저장한다 — 같은 이름이 둘로 보이거나 정렬이 어긋나지 않게.
"""

from __future__ import annotations

import unicodedata

EXTS = {"video": (".mp4", ".mkv"), "audio": (".m4a", ".aac")}
MAX_BYTES = 240


def normalize(name: str) -> str:
    """맥(NFD) 한글 이름을 NFC 로 합치고 앞뒤 공백을 뗀다."""
    return unicodedata.normalize("NFC", name or "").strip()


def problems(name: str, kind: str) -> list[str]:
    """이름이 규칙에 맞지 않으면 한국어 사유 목록. 문제없으면 빈 목록."""
    out: list[str] = []
    if not name:
        return ["파일 이름이 비어 있습니다"]
    if name.startswith("."):
        out.append("점(.)으로 시작하는 이름은 숨김 파일이라 쓸 수 없습니다")
    if "#{" in name:
        out.append("이름에 '#{' 를 쓸 수 없습니다 (음악 프로그램이 특수 기호로 읽음)")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in name):
        out.append("이름에 줄바꿈·탭 같은 보이지 않는 문자가 들어 있습니다")
    exts = EXTS.get(kind, ())
    if exts and not name.lower().endswith(exts):
        out.append(f"허용 확장자는 {', '.join(exts)} 입니다")
    n = len(name.encode("utf-8"))
    if n > MAX_BYTES:
        out.append(f"이름이 너무 깁니다 ({n}바이트) — 한글 약 80자, 영문 {MAX_BYTES}자까지")
    return out
