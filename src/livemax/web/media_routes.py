"""미디어 관리 — /media/video, /media/audio (DESIGN 8.3 ③)

업로드는 절대 방송 폴더에 직접 쓰지 않는다 (DESIGN 11-3):
    브라우저 → _incoming/.tmp/<uuid>.part (스트리밍 기록)
            → 완료 시 rename → _incoming/<kind>/<폴더>/<파일>
            → livemax-ingest 가 ffprobe 검증 후 rename 으로 방송 폴더에 이동
삭제:
    음원 — 즉시 삭제 (Liquidsoap 이 폴더 감시로 목록에서 뺀다)
    영상 — 오늘의 concat 목록이 참조 중이면 '삭제 예약' 후, 참조가 끝나면 cleanup 이 실행
           (재생 예정 클립을 지우면 ffmpeg 가 그 지점에서 멈추기 때문)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from .. import catalog, config, db, thumbs
from ..cleanup import _referenced_by_concat
from . import data
from .common import redirect, render

log = logging.getLogger("livemax.web.media")
router = APIRouter()
KINDS = ("video", "audio")
EXTS = {"video": (".mp4", ".mkv"), "audio": (".m4a", ".aac")}


def _kind_ok(kind: str) -> bool:
    return kind in KINDS


def _safe_filename(name: str) -> str | None:
    name = Path(name).name.strip()
    if not name or name.startswith(".") or len(name) > 180 or "\x00" in name or "#{" in name:
        return None
    if not name.lower().endswith(EXTS["video"] + EXTS["audio"]):
        return None
    return name


@router.get("/media/{kind}")
def media_page(request: Request, kind: str, folder: str = ""):
    if not _kind_ok(kind):
        return redirect("/")
    conn, server, paths = request.state.conn, request.state.server, request.state.paths
    lives, _ = config.load_all_lives(server=server, cross=False)
    usage = data.folder_usage(lives, kind)
    folders = []
    for f in catalog.list_folders(paths, kind):
        folders.append({"name": f, "count": len(catalog.list_files(paths, kind, f)), "used_by": usage.get(f, [])})
    files = data.media_listing(conn, paths, kind, folder) if folder and config.valid_folder_name(folder) else []
    rejected = [
        {"ts": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])), "folder": r["folder"], "name": r["name"],
         "reasons": json.loads(r["reasons"])}
        for r in conn.execute("SELECT * FROM rejections WHERE kind=? ORDER BY ts DESC LIMIT 50", (kind,)).fetchall()
    ]
    spec = server["spec"][kind]
    return render(request, "media.html", kind=kind, folders=folders, folder=folder, files=files, rejected=rejected,
                  spec=spec, missing=[f for f in usage if f not in {x["name"] for x in folders}],
                  max_mb=server["dashboard"]["max_upload_mb"])


@router.post("/media/{kind}/folders")
def create_folder(request: Request, kind: str, name: str = Form(...)):
    paths = request.state.paths
    name = name.strip()
    if not _kind_ok(kind) or not config.valid_folder_name(name):
        return redirect(f"/media/{kind}", err="폴더 이름은 문자/숫자로 시작하고 문자·숫자·공백·._-() 만 쓸 수 있습니다")
    (paths.kind_root(kind) / name).mkdir(parents=True, exist_ok=True)
    (paths.incoming / kind / name).mkdir(parents=True, exist_ok=True)
    db.add_event(request.state.conn, "info", "dashboard", f"폴더 생성 {kind}/{name}")
    return redirect(f"/media/{kind}?folder={name}", ok=f"폴더 '{name}' 생성")


@router.post("/media/{kind}/folders/{name}/rename")
def rename_folder(request: Request, kind: str, name: str, new_name: str = Form(...)):
    paths, server = request.state.paths, request.state.server
    new_name = new_name.strip()
    if not _kind_ok(kind) or not config.valid_folder_name(name) or not config.valid_folder_name(new_name):
        return redirect(f"/media/{kind}", err="폴더 이름 오류")
    lives, _ = config.load_all_lives(server=server, cross=False)
    used = data.folder_usage(lives, kind).get(name)
    if used:
        # 이름을 바꾸면 편성(YAML)이 깨진다. 먼저 라이브 설정에서 폴더를 빼야 한다.
        return redirect(f"/media/{kind}", err=f"'{name}' 은 {', '.join(used)} 편성에서 사용 중 — 먼저 라이브 설정에서 제외하세요")
    src, dst = paths.kind_root(kind) / name, paths.kind_root(kind) / new_name
    if dst.exists():
        return redirect(f"/media/{kind}", err="같은 이름의 폴더가 이미 있습니다")
    os.rename(src, dst)
    inc = paths.incoming / kind / name
    if inc.is_dir():
        os.rename(inc, paths.incoming / kind / new_name)
    th = paths.thumbs_dir / kind / name
    if th.is_dir():
        os.rename(th, paths.thumbs_dir / kind / new_name)
    conn = request.state.conn
    for r in conn.execute("SELECT path FROM media_files WHERE kind=? AND folder=?", (kind, name)).fetchall():
        newp = str(dst / Path(r["path"]).name)
        conn.execute("UPDATE media_files SET path=?, folder=?, thumb=REPLACE(thumb, ?, ?) WHERE path=?",
                     (newp, new_name, f"{kind}/{name}/", f"{kind}/{new_name}/", r["path"]))
    db.add_event(conn, "info", "dashboard", f"폴더 이름 변경 {kind}/{name} → {new_name}")
    return redirect(f"/media/{kind}?folder={new_name}", ok="이름 변경 완료")


@router.post("/media/{kind}/folders/{name}/delete")
def delete_folder(request: Request, kind: str, name: str):
    paths, server = request.state.paths, request.state.server
    if not _kind_ok(kind) or not config.valid_folder_name(name):
        return redirect(f"/media/{kind}", err="폴더 이름 오류")
    lives, _ = config.load_all_lives(server=server, cross=False)
    used = data.folder_usage(lives, kind).get(name)
    if used:
        return redirect(f"/media/{kind}", err=f"'{name}' 은 {', '.join(used)} 편성에서 사용 중이라 삭제할 수 없습니다")
    if catalog.list_files(paths, kind, name):
        return redirect(f"/media/{kind}?folder={name}", err="폴더가 비어 있지 않습니다. 파일을 먼저 삭제하세요")
    shutil.rmtree(paths.kind_root(kind) / name, ignore_errors=True)
    shutil.rmtree(paths.incoming / kind / name, ignore_errors=True)
    db.add_event(request.state.conn, "info", "dashboard", f"폴더 삭제 {kind}/{name}")
    return redirect(f"/media/{kind}", ok=f"폴더 '{name}' 삭제")


@router.put("/media/{kind}/upload/{folder}")
async def upload(request: Request, kind: str, folder: str, name: str):
    """원시 바디 스트리밍 업로드 (XHR 진행률 표시용). 메모리에 올리지 않고 바로 디스크에 쓴다."""
    paths, server = request.state.paths, request.state.server
    if not _kind_ok(kind) or not config.valid_folder_name(folder) or not (paths.kind_root(kind) / folder).is_dir():
        return JSONResponse({"ok": False, "error": "폴더 없음"}, status_code=400)
    fname = _safe_filename(name)
    if not fname or not fname.lower().endswith(EXTS[kind]):
        return JSONResponse({"ok": False, "error": f"허용 확장자: {', '.join(EXTS[kind])}"}, status_code=400)
    max_bytes = int(server["dashboard"]["max_upload_mb"]) * 1024 * 1024
    paths.incoming_tmp.mkdir(parents=True, exist_ok=True)
    tmp = paths.incoming_tmp / f"{uuid.uuid4().hex}.part"
    written = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("too large")
                f.write(chunk)
        dest_dir = paths.incoming / kind / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / fname
        if dest.exists():
            dest = dest_dir / f"{Path(fname).stem}-{uuid.uuid4().hex[:6]}{Path(fname).suffix}"
        os.rename(tmp, dest)  # 완료된 파일만 스테이징에 나타난다
    except ValueError:
        tmp.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": f"최대 {server['dashboard']['max_upload_mb']}MB"}, status_code=413)
    except Exception as e:  # noqa: BLE001 — 끊긴 전송은 즉시 삭제 (DESIGN 6.4)
        tmp.unlink(missing_ok=True)
        log.warning("업로드 실패 %s: %s", fname, e)
        return JSONResponse({"ok": False, "error": "전송 중단"}, status_code=400)
    log.info("업로드 수신 %s/%s/%s (%d bytes) → 검증 대기", kind, folder, fname, written)
    return {"ok": True, "name": dest.name, "bytes": written, "message": "업로드 완료 — 규격 검증 대기 중 (수십 초)"}


@router.post("/media/{kind}/files/delete")
def delete_file(request: Request, kind: str, folder: str = Form(...), name: str = Form(...), confirm: str = Form("")):
    conn, paths, server = request.state.conn, request.state.paths, request.state.server
    if not _kind_ok(kind) or not config.valid_folder_name(folder) or "/" in name:
        return redirect(f"/media/{kind}", err="잘못된 요청")
    p = paths.kind_root(kind) / folder / name
    if not p.is_file():
        return redirect(f"/media/{kind}?folder={folder}", err="파일 없음")

    lives, _ = config.load_all_lives(server=server, cross=False)
    used = data.folder_usage(lives, kind).get(folder, [])
    remaining = len(catalog.list_files(paths, kind, folder))
    if used and remaining <= 1 and confirm != "yes":
        # 사용 중인 폴더의 마지막 파일 — 경고 후 진행 (DESIGN 8.3)
        return render(request, "confirm.html", kind=kind, folder=folder, name=name, used=used)

    if kind == "video" and str(p) in _referenced_by_concat(paths):
        conn.execute("INSERT OR IGNORE INTO pending_deletes(path, requested_at) VALUES (?,?)", (str(p), time.time()))
        db.add_event(conn, "info", "dashboard", f"영상 삭제 예약 {folder}/{name} (오늘 편성에서 빠지면 삭제)")
        return redirect(f"/media/{kind}?folder={folder}",
                        ok="오늘 편성 목록이 이 클립을 사용 중이라 '삭제 예약'했습니다. 다음 재시작 이후 자동으로 삭제됩니다")
    p.unlink()
    catalog.remove(conn, p)
    if kind == "video":
        thumbs.thumb_path(paths, folder, name).unlink(missing_ok=True)
    db.add_event(conn, "info", "dashboard", f"파일 삭제 {kind}/{folder}/{name}")
    return redirect(f"/media/{kind}?folder={folder}", ok=f"'{name}' 삭제")
