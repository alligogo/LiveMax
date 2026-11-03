"""업로드 처리 서비스 (DESIGN 4.2) — livemax-ingest.service

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

import argparse
import json
import logging
import os
import signal
import time
from pathlib import Path

from . import catalog, config, db, logsetup, probe, thumbs
from .paths import Paths

log = logging.getLogger("livemax.ingest")

PARTIAL_SUFFIXES = (".part", ".tmp", ".filepart", ".crdownload", ".partial")
KINDS = ("video", "audio")


def ensure_dirs(paths: Paths) -> None:
    for d in (paths.incoming, paths.incoming_tmp, paths.rejected, paths.video, paths.audio,
              paths.incoming / "video", paths.incoming / "audio"):
        d.mkdir(parents=True, exist_ok=True)


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    for i in range(1, 1000):
        cand = dest.with_name(f"{stem}-{i}{suf}")
        if not cand.exists():
            return cand
    raise FileExistsError(dest)


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

    dest = _unique_dest(target_dir / src.name)

    thumb_rel = None
    if kind == "video":
        # 썸네일은 이동 전에 스테이징 파일에서 1회 생성 (DESIGN 8.4)
        tp = thumbs.thumb_path(paths, folder, dest.name)
        if thumbs.make_thumbnail(src, tp):
            thumb_rel = str(tp.relative_to(paths.thumbs_dir))

    os.rename(src, dest)  # 원자적 이동 — 중간 상태가 존재하지 않는다
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LiveMax 업로드 검증·이동 서비스")
    ap.add_argument("--once", action="store_true", help="한 번만 훑고 종료")
    args = ap.parse_args(argv)

    server = config.load_server()
    paths = config.get_paths(server)
    logsetup.setup("ingest", server)
    ensure_dirs(paths)
    db.init(paths.db)
    conn = db.connect(paths.db)

    stop = False

    def _sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    seen: dict[Path, tuple[int, float, float]] = {}
    log.info("ingest 시작: %s", paths.incoming)
    if args.once:
        # --once: 안정 판정 대기 없이 즉시 처리 (수동 실행용)
        server = {**server, "ingest": {**server["ingest"], "stable_seconds": 0}}
        scan_once(paths, server, conn, seen)
        scan_once(paths, server, conn, seen)
        return 0
    while not stop:
        try:
            scan_once(paths, server, conn, seen)
        except Exception:  # noqa: BLE001
            log.exception("scan 실패")
        for _ in range(int(server["ingest"]["poll_seconds"] * 10)):
            if stop:
                break
            time.sleep(0.1)
    log.info("ingest 종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
