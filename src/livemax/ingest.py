"""업로드 처리 (DESIGN 4.2) — livemaxd 의 ingest 스레드가 scan_once 를 주기적으로 호출

    업로드 → _incoming/<video|audio>/<폴더>/<파일>
          → (전송 완료 판정: 크기·mtime 이 stable_seconds 동안 불변)
          → ffprobe 규격 검증
          → 통과: os.rename 으로 /media/<kind>/<폴더>/ 에 이동 (같은 파일시스템 = 원자적)
          → 실패: _rejected/<kind>/<폴더>/ 로 이동 + 사유 기록

절대 방송 중인 폴더에 직접 쓰지 않는다 (DESIGN 11-3). Liquidsoap 이 반쪽 파일을 집지 않도록
이동은 반드시 rename 한 번으로 끝낸다. 다른 파일시스템이면 복사하지 않고 거부한다.

이 서비스는 systemd 에서 IOSchedulingClass=idle (= ionice -c3) 로 돌아 송출 읽기를 방해하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import catalog, config, db, filenames, probe, thumbs
from .paths import Paths

log = logging.getLogger("livemax.ingest")

PARTIAL_SUFFIXES = (".part", ".tmp", ".filepart", ".crdownload", ".partial")
KINDS = ("video", "audio")


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    for i in range(1, 1000):
        cand = dest.with_name(f"{stem}-{i}{suf}")
        if not cand.exists():
            return cand
    raise FileExistsError(dest)


def collision(src: Path, target_dir: Path) -> list[str]:
    """방송 폴더에 같은 이름(대소문자 무시)의 파일이 있으면 거부 사유.

    규칙: 덮어쓰지도, 번호를 붙여 따로 넣지도 않는다. 그 파일이 지금 송출 중일 수 있으므로
    교체하려면 웹에서 기존 파일을 먼저 지우고(송출중·다음 곡이면 잠김) 다시 올린다.
    (내용이 같은 중복 검사는 파일 전체를 읽어 CPU 를 많이 써서 뺐다 — 중복 관리는 올리는 사람 몫)
    """
    if not target_dir.is_dir():
        return []
    name = src.name.lower()
    for f in target_dir.iterdir():
        if f.is_file() and not f.name.startswith(".") and f.name.lower() == name:
            return [f"같은 이름의 파일 '{f.name}' 이 이미 있습니다 — 교체하려면 웹에서 기존 파일을 지운 뒤 "
                    f"다시 올리거나, 이름을 바꿔 올리세요 (송출 중인 파일은 덮어쓰지 않습니다)"]
    return []


def _same_fs(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def reject(paths: Paths, conn, kind: str, folder: str, src: Path, reasons: list[str]) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest_dir = paths.rejected / kind / (folder or "_unknown")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir / f"{stamp}_{src.name}")
    os.rename(src, dest)
    # 사유는 DB + 로그에 남긴다 (격리 파일은 7일 뒤 삭제되어도 사유 기록은 유지)
    conn.execute(
        "INSERT INTO rejections(ts, kind, folder, name, reasons, rejected_path) VALUES (?,?,?,?,?,?)",
        (time.time(), kind, folder, src.name, json.dumps(reasons, ensure_ascii=False), str(dest)),
    )
    db.add_event(conn, "warn", "ingest", f"[{kind}/{folder}] {src.name} 거부: " + "; ".join(reasons))
    log.warning("거부 %s/%s/%s → %s | 사유: %s", kind, folder, src.name, dest, " / ".join(reasons))
    return dest


def process_file(paths: Paths, server: dict, conn, kind: str, folder: str, src: Path) -> tuple[bool, list[str]]:
    """스테이징 파일 1개를 검증하고 이동한다. (통과 여부, 사유) 반환."""
    log.info("검증 시작 %s/%s/%s (%d bytes)", kind, folder, src.name, src.stat().st_size)
    target_dir = paths.kind_root(kind) / folder

    if not config.valid_folder_name(folder):
        reasons = [f"폴더 이름 '{folder}' 사용 불가"]
        reject(paths, conn, kind, folder, src, reasons)
        return False, reasons
    if not target_dir.is_dir():
        reasons = [f"대상 폴더 {kind}/{folder} 가 없음 — 대시보드에서 먼저 폴더를 만드세요"]
        reject(paths, conn, kind, folder, src, reasons)
        return False, reasons

    bad = filenames.problems(src.name, kind)
    if bad:
        reject(paths, conn, kind, folder, src, bad)
        return False, bad
    nfc = filenames.normalize(src.name)
    if nfc != src.name:  # 맥에서 올린 한글(NFD) → 합친 이름으로. 대기 폴더 안에서만 바꾼다
        if (src.parent / nfc).exists():
            reasons = [f"같은 이름의 파일 '{nfc}' 이 이미 검증 대기 중입니다"]
            reject(paths, conn, kind, folder, src, reasons)
            return False, reasons
        new = src.parent / nfc
        os.rename(src, new)
        log.info("이름을 NFC 로 합침: %r → %r", src.name, nfc)
        src = new

    dup = collision(src, target_dir)
    if dup:
        reject(paths, conn, kind, folder, src, dup)
        return False, dup

    t0 = time.monotonic()
    result = probe.validate(kind, src, server)
    log.debug("ffprobe %s: ok=%s dur=%s br=%s info=%s warn=%s (%.2fs)", src.name, result.ok, result.duration,
              result.bitrate_kbps, result.info, result.warnings, time.monotonic() - t0)
    if not result.ok:
        reject(paths, conn, kind, folder, src, result.reasons)
        return False, result.reasons

    if not _same_fs(src.parent, target_dir):
        reasons = ["_incoming 과 대상 폴더가 다른 디스크에 있음 — 원자적 이동 불가 (설치 구성 오류)"]
        reject(paths, conn, kind, folder, src, reasons)
        log.error(reasons[0])
        return False, reasons

    dest = target_dir / src.name
    if dest.exists():  # 검증하는 사이 같은 이름이 생긴 경우 (거의 없음) — 그래도 덮어쓰지 않는다
        reasons = collision(src, target_dir) or [f"같은 이름의 파일 '{src.name}' 이 이미 있습니다"]
        reject(paths, conn, kind, folder, src, reasons)
        return False, reasons

    thumb_rel = None
    if kind == "video":
        # 썸네일은 이동 전에 스테이징 파일에서 1회 생성 (DESIGN 8.4)
        tp = thumbs.thumb_path(paths, folder, dest.name)
        if thumbs.make_thumbnail(src, tp):
            thumb_rel = str(tp.relative_to(paths.thumbs_dir))

    try:
        os.link(src, dest)  # 원자적 + 이미 있으면 실패 (os.rename 은 조용히 덮어쓴다)
    except FileExistsError:
        if kind == "video" and thumb_rel:
            thumbs.thumb_path(paths, folder, dest.name).unlink(missing_ok=True)
        reasons = [f"같은 이름의 파일 '{src.name}' 이 이미 있습니다"]
        reject(paths, conn, kind, folder, src, reasons)
        return False, reasons
    os.unlink(src)
    catalog.upsert(conn, kind, folder, dest, result.duration, result.bitrate_kbps, result.info, thumb_rel)
    msg = f"[{kind}/{folder}] {dest.name} 추가 ({result.duration or 0:.1f}s, {result.bitrate_kbps or 0:.0f}k)"
    if result.warnings:
        msg += " | 참고: " + "; ".join(result.warnings)
    db.add_event(conn, "info", "ingest", msg)
    log.info("통과 → %s %s", dest, result.warnings or "")
    return True, []


def scan_once(paths: Paths, server: dict, conn, seen: dict[Path, tuple[int, float, float]]) -> int:
    """_incoming 을 한 번 훑어 전송이 끝난 파일을 처리한다."""
    stable = float(server["ingest"]["stable_seconds"])
    now = time.time()
    handled = 0
    for kind in KINDS:
        base = paths.incoming / kind
        if not base.is_dir():
            continue
        for folder_dir in base.iterdir():
            if not folder_dir.is_dir():
                # 폴더 없이 바로 올린 파일 → 어느 폴더인지 모르므로 거부
                if folder_dir.is_file() and not folder_dir.name.startswith("."):
                    if _is_stable(folder_dir, seen, now, stable):
                        reject(paths, conn, kind, "", folder_dir, [f"_incoming/{kind}/<폴더>/ 아래에 올려야 함"])
                continue
            for f in folder_dir.iterdir():
                if not f.is_file() or f.name.startswith(".") or f.name.lower().endswith(PARTIAL_SUFFIXES):
                    continue
                if not _is_stable(f, seen, now, stable):
                    continue
                seen.pop(f, None)
                if handled:  # 여러 개를 한꺼번에 올려도 한 개씩, 사이사이 쉬면서 (송출에 CPU 양보)
                    time.sleep(float(server["ingest"].get("pause_seconds", 0)))
                try:
                    process_file(paths, server, conn, kind, folder_dir.name, f)
                except Exception:  # noqa: BLE001
                    log.exception("처리 중 오류: %s", f)
                handled += 1
    # 사라진 파일은 추적 목록에서 제거
    for p in [p for p in seen if not p.exists()]:
        seen.pop(p, None)
    return handled


def _is_stable(f: Path, seen: dict[Path, tuple[int, float, float]], now: float, stable: float) -> bool:
    try:
        st = f.stat()
    except FileNotFoundError:
        return False
    sig = (st.st_size, st.st_mtime)
    prev = seen.get(f)
    if prev is None or (prev[0], prev[1]) != sig:
        seen[f] = (sig[0], sig[1], now)
        return False
    return now - prev[2] >= stable and now - st.st_mtime >= stable


# ---------------------------------------------------------------------------
# 방송 폴더 감시 — WinSCP 등으로 _incoming 이 아닌 방송 폴더에 직접 넣거나 바꾼 파일
# ---------------------------------------------------------------------------
def watch_broadcast(paths: Paths, server: dict, conn, memo: dict) -> int:
    """방송 폴더의 파일을 카탈로그와 비교한다. 원본은 절대 옮기거나 지우지 않는다 (송출 중일 수 있음).

    - 카탈로그에 없는 새 파일: 규격 검사 → 맞으면 등록(썸네일 포함), 안 맞으면 경고만
    - 크기·수정 시각이 바뀐 파일: 경고 (직접 덮어쓴 것) + 길이 다시 읽기
    경고는 이벤트(source=media-watch)로 남아 웹 알림·휴대폰 알림으로 간다. 같은 상태는 한 번만.
    """
    now, n = time.time(), 0
    for kind in KINDS:
        for folder in catalog.list_folders(paths, kind):
            for f in catalog.list_files(paths, kind, folder):
                try:
                    st = f.stat()
                except OSError:
                    continue
                if now - st.st_mtime < 60:  # 아직 쓰는 중일 수 있음
                    continue
                sig = (st.st_size, int(st.st_mtime))
                if memo.get(f) == sig:
                    continue
                row = conn.execute("SELECT size, mtime, info FROM media_files WHERE path=?", (str(f),)).fetchone()
                known = row and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1
                if known and row["info"] not in (None, "", "{}"):
                    memo[f] = sig
                    continue
                result = probe.validate(kind, f, server)
                if row and not known:
                    db.add_event(conn, "warn", "media-watch",
                                 f"[{kind}/{folder}] {f.name} 이(가) 방송 폴더에서 직접 바뀌었습니다 — 송출 중이면 튈 수 있어요. "
                                 f"다음부터는 웹이나 _incoming 으로 올리세요" + ("" if result.ok else " (규격 불일치: " + "; ".join(result.reasons) + ")"))
                elif not row:
                    db.add_event(conn, "warn", "media-watch",
                                 f"[{kind}/{folder}] {f.name} 을(를) 방송 폴더에서 발견 — 검사 없이 직접 넣은 파일" +
                                 (" (규격 통과, 등록함)" if result.ok else " (규격 불일치: " + "; ".join(result.reasons) + " — 웹에서 지우세요)"))
                thumb_rel = None
                if result.ok and kind == "video":
                    tp = thumbs.thumb_path(paths, folder, f.name)
                    if thumbs.make_thumbnail(f, tp):
                        thumb_rel = str(tp.relative_to(paths.thumbs_dir))
                catalog.upsert(conn, kind, folder, f, result.duration, result.bitrate_kbps, result.info, thumb_rel)
                memo[f] = sig
                n += 1
    for p in [p for p in memo if not p.exists()]:
        memo.pop(p, None)
    return n
