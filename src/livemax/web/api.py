"""웹 API + 화면(SPA) — 브라우저에서 모든 운영을 한다 (v2).

외부 접속은 Caddy(HTTPS) → 127.0.0.1:8080. 시각은 전부 epoch 초로 내보내고 브라우저가 현지 시간으로 표시한다.
보안: argon2 비밀번호, IP별 5회 실패 15분 차단, HttpOnly+Secure+SameSite=Strict 세션, 같은 출처만 변경 허용.
스트림 키는 응답 어디에도 평문으로 나가지 않는다 (****1a2b).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import capacity, catalog, config, db, secrets, thumbs
from ..cleanup import _referenced_by_concat
from ..config import ConfigError, Live, live_from_dict
from . import auth

log = logging.getLogger("livemax.web")
STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "::1"}
REBOOT_FLAG = Path("/var/run/reboot-required")
EXTS = {"video": (".mp4", ".mkv"), "audio": (".m4a", ".aac")}


def err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    if peer in LOOPBACK and xff:
        return xff.split(",")[-1].strip()
    return peer


def create_app(core) -> FastAPI:  # core = daemon.App
    paths, settings, sup = core.paths, core.settings, core.supervisor
    store = sup.store
    app = FastAPI(title="LiveMax", docs_url=None, redoc_url=None, openapi_url=None)
    initial = auth.ensure_initial_password()
    if initial:
        log.warning("관리자 초기 비밀번호를 생성했습니다: %s", auth.initial_file())

    # ------------------------------------------------------------------ 미들웨어
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            resp = await call_next(request)
            resp.headers["X-Frame-Options"] = "DENY"
            return resp
        conn = db.connect(paths.db)
        request.state.conn = conn
        request.state.ip = client_ip(request)
        try:
            if request.method not in ("GET", "HEAD"):
                origin = request.headers.get("origin") or request.headers.get("referer") or ""
                if urlparse(origin).netloc != request.headers.get("host", ""):
                    return err("잘못된 요청 출처", 403)
            if path not in ("/api/session", "/api/login"):
                sess = auth.get_session(conn, request.cookies.get(auth.COOKIE))
                if not sess:
                    return err("로그인이 필요합니다", 401)
                request.state.session = sess
                if auth.must_change() and path not in ("/api/password", "/api/logout"):
                    return err("초기 비밀번호를 먼저 변경하세요", 428)
            resp = await call_next(request)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        finally:
            conn.close()

    # ------------------------------------------------------------------ 로그인
    @app.get("/api/session")
    def session(request: Request):
        sess = auth.get_session(request.state.conn, request.cookies.get(auth.COOKIE))
        return {"authenticated": bool(sess), "must_change": auth.must_change(), "version": core.version,
                "username": settings["dashboard"]["username"]}

    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        conn, ip, d = request.state.conn, request.state.ip, settings["dashboard"]
        remain = auth.blocked_seconds(conn, ip, int(d["login_max_failures"]), int(d["login_block_minutes"]))
        if remain:
            return err(f"로그인 실패가 많아 {remain // 60 + 1}분간 차단되었습니다", 429)
        user, pw = str(body.get("username", "")), str(body.get("password", ""))
        ok = user == d["username"] and auth.verify_password(pw)
        auth.record_attempt(conn, ip, user, ok)
        if not ok:
            return err("아이디 또는 비밀번호가 올바르지 않습니다", 401)
        token = auth.create_session(conn, ip, request.headers.get("user-agent", ""), float(d["session_hours"]))
        resp = JSONResponse({"ok": True, "must_change": auth.must_change()})
        resp.set_cookie(auth.COOKIE, token, httponly=True, secure=True, samesite="strict",
                        max_age=int(float(d["session_hours"]) * 3600), path="/")
        return resp

    @app.post("/api/logout")
    def logout(request: Request):
        auth.revoke(request.state.conn, request.state.session["token_hash"])
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.post("/api/password")
    async def password(request: Request):
        b = await request.json()
        cur, new = str(b.get("current", "")), str(b.get("new", ""))
        if not auth.verify_password(cur):
            return err("현재 비밀번호가 틀립니다")
        probs = auth.password_problems(new, settings["dashboard"]["username"])
        if new == cur:
            probs.append("이전과 다른 비밀번호를 쓰세요")
        if probs:
            return err(" / ".join(probs))
        auth.set_password(new)
        auth.revoke_all_except(request.state.conn, request.state.session["token_hash"])
        return {"ok": True}

    # ------------------------------------------------------------------ 개요
    def live_view(lv: Live, snap: dict) -> dict:
        s = snap.get(lv.id, {})
        state = s.get("state", "stopped")
        speed = s.get("speed")
        if not lv.enabled and state == "stopped":
            kind = "stopped"
        elif state == "running":
            wd = settings["watchdog"]
            if speed is not None and speed < float(wd["speed_min"]) or (s.get("restarts_hour") or 0) >= 3:
                kind = "warn"
            else:
                kind = "ok"
        elif state in ("starting", "restarting"):
            kind = "restarting"
        else:
            kind = "down"
        np_ = s.get("nowplaying") or {}
        key_path = paths.stream_key(lv.id)
        key_masked = secrets.mask(key_path.read_text().strip()) if key_path.exists() else ""
        return {
            **lv.to_dict(), "status": kind, "state": state, "message": s.get("message", ""),
            "speed": speed, "bitrate_kbps": s.get("bitrate"), "out_time": s.get("out_time"),
            "started_at": s.get("ffmpeg_started_at") if state == "running" else None,
            "transport": s.get("transport"), "clip": s.get("clip"), "clip_folder": s.get("clip_folder"),
            "song_title": np_.get("title") or (Path(np_.get("file", "")).stem if np_.get("file") else ""),
            "song_artist": np_.get("artist") or "", "restarts_hour": s.get("restarts_hour", 0),
            "restarts": s.get("restarts", []), "next_restart": lv.next_restart(settings).timestamp(),
            "key_masked": key_masked, "cpu": (core.collector.per_live.get(lv.id) or {}).get("cpu"),
            "rss_mb": (core.collector.per_live.get(lv.id) or {}).get("rss_mb"),
        }

    def overview_data(conn) -> dict:
        snap = sup.snapshot()
        lives = [live_view(lv, snap) for lv in store.all()]
        sysd = dict(core.collector.latest)
        cap = capacity.compute(settings["capacity"], [config.parse_bitrate(lv["bitrate"]) for lv in lives if lv["enabled"]],
                               sysd.get("month_tx_bytes"), sysd.get("net_tx_mbps"), sysd.get("cpu_pct"),
                               sysd.get("mem_used_mb"), sysd.get("mem_total_mb"))
        alerts = []
        for lv in lives:
            if lv["status"] in ("warn", "down"):
                alerts.append({"level": "warn" if lv["status"] == "warn" else "error", "ts": time.time(),
                               "live": lv["id"], "message": lv["message"] or ("speed 저하" if lv["status"] == "warn" else "중단")})
        if sysd.get("disk_pct") and sysd["disk_pct"] >= settings["cleanup"]["disk_warn_percent"]:
            alerts.append({"level": "warn", "ts": time.time(), "live": None, "message": f"디스크 {sysd['disk_pct']:.0f}% 사용 중"})
        if REBOOT_FLAG.exists():
            alerts.append({"level": "warn", "ts": REBOOT_FLAG.stat().st_mtime, "live": None,
                           "message": "서버 재부팅 필요 (보안 업데이트 적용 대기) — 설정 > 시스템"})
        for e in conn.execute("SELECT * FROM events WHERE level IN ('warn','error') AND ts > ? ORDER BY ts DESC LIMIT 20",
                              (time.time() - 86400,)).fetchall():
            alerts.append({"level": e["level"], "ts": e["ts"], "live": e["live_id"], "message": e["message"]})
        return {
            "server_time": time.time(), "version": core.version, "uptime_since": core.started_at,
            "lives": lives, "system": sysd,
            "capacity": {"can_add": cap.can_add, "limiting": cap.limiting, "projected_tb": cap.projected_tb,
                         "limit_tb": cap.limit_tb, "usage_ratio": cap.usage_ratio, "month_tx_tb": cap.month_tx_tb,
                         "per_live_tb": cap.per_live_tb, "per_live_mbps": cap.per_live_mbps},
            "alerts": alerts[:30], "reboot_required": REBOOT_FLAG.exists(),
        }

    @app.get("/api/overview")
    def overview(request: Request):
        return overview_data(request.state.conn)

    # ------------------------------------------------------------------ 라이브
    async def _live_payload(request: Request, lv: Live) -> tuple[Live, str, str]:
        b = await request.json()
        lv.name = str(b.get("name", lv.name)).strip()[:80]
        lv.video_folders = [str(x) for x in b.get("video_folders", lv.video_folders)]
        lv.audio_folders = [str(x) for x in b.get("audio_folders", lv.audio_folders)]
        lv.audio_mode = str(b.get("audio_mode", lv.audio_mode))
        lv.norepeat_count = int(b.get("norepeat_count", lv.norepeat_count))
        lv.crossfade = float(b.get("crossfade", lv.crossfade) or 0)
        lv.bitrate = str(b.get("bitrate", lv.bitrate)).strip()
        lv.youtube_video_id = str(b.get("youtube_video_id", lv.youtube_video_id) or "").strip()
        return lv, str(b.get("stream_key", "") or "").strip(), str(b.get("apply", "now"))

    @app.post("/api/lives")
    async def live_create(request: Request):
        b = await request.json()
        lv = store.new(str(b.get("name", "")).strip()[:80])
        try:
            lv, key, apply = await _live_payload(request, lv)
            if not key:
                return err("스트림 키를 입력하세요")
            store.save(lv)
        except (ConfigError, ValueError) as e:
            return err(str(e))
        secrets.write_secret(paths.stream_key(lv.id), key)
        db.add_event(request.state.conn, "info", "web", f"라이브 생성: {lv.name}", lv.id)
        if apply == "now":
            sup.start(lv.id)
        return {"ok": True, "id": lv.id}

    @app.put("/api/lives/{live_id}")
    async def live_update(request: Request, live_id: str):
        lv = store.get(live_id)
        if not lv:
            return err("라이브 없음", 404)
        try:
            lv, key, apply = await _live_payload(request, lv)
            store.save(lv)
        except (ConfigError, ValueError) as e:
            return err(str(e))
        if key:
            secrets.write_secret(paths.stream_key(lv.id), key)
        db.add_event(request.state.conn, "info", "web", "설정 변경" + (" (스트림 키 교체)" if key else ""), lv.id)
        if apply == "now" and lv.enabled:
            sup.restart(lv.id, "설정 적용", audio_too=True)
        return {"ok": True}

    @app.delete("/api/lives/{live_id}")
    def live_delete(request: Request, live_id: str):
        if not store.get(live_id):
            return err("라이브 없음", 404)
        sup.remove(live_id)
        db.add_event(request.state.conn, "info", "web", "라이브 삭제", live_id)
        return {"ok": True}

    @app.post("/api/lives/{live_id}/{action}")
    def live_action(request: Request, live_id: str, action: str):
        try:
            if action == "start":
                sup.start(live_id)
            elif action == "stop":
                sup.stop(live_id)
            elif action == "restart":
                sup.restart(live_id, "사용자 재시작", audio_too=True)
            else:
                return err("알 수 없는 동작", 404)
        except ConfigError as e:
            return err(str(e))
        return {"ok": True}

    @app.get("/api/logs")
    def logs(name: str = "livemaxd", lines: int = 200):
        if not (name == "livemaxd" or any(name == f"{p}-{lv.id}" for lv in store.all() for p in ("live", "audio", "tls"))):
            return err("로그 없음", 404)
        f = paths.logs / f"{name}.log"
        if not f.exists():
            return {"name": name, "text": "(아직 기록 없음)"}
        lines = max(20, min(int(lines), 2000))
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - lines * 250))
            text = b"\n".join(fh.read().splitlines()[-lines:]).decode("utf-8", "replace")
        return {"name": name, "text": secrets.redact(text)}

    # ------------------------------------------------------------------ 미디어
    def _kind(kind: str) -> bool:
        return kind in ("video", "audio")

    @app.get("/api/media/{kind}")
    def media(request: Request, kind: str):
        if not _kind(kind):
            return err("잘못된 종류", 404)
        usage = store.folder_usage(kind)
        folders = []
        for f in catalog.list_folders(paths, kind):
            files = catalog.list_files(paths, kind, f)
            pend = paths.incoming / kind / f
            folders.append({"name": f, "count": len(files), "size": sum(p.stat().st_size for p in files),
                            "used_by": usage.get(f, []),
                            "pending": len([p for p in pend.iterdir() if p.is_file()]) if pend.is_dir() else 0})
        rej = [{"ts": r["ts"], "folder": r["folder"], "name": r["name"], "reasons": json.loads(r["reasons"])}
               for r in request.state.conn.execute(
                   "SELECT * FROM rejections WHERE kind=? ORDER BY ts DESC LIMIT 50", (kind,)).fetchall()]
        return {"folders": folders, "rejected": rej, "spec": settings["spec"][kind]}

    @app.get("/api/media/{kind}/{folder}")
    def media_folder(request: Request, kind: str, folder: str):
        if not _kind(kind) or not config.valid_folder_name(folder):
            return err("잘못된 폴더", 404)
        conn = request.state.conn
        out = []
        for p in catalog.list_files(paths, kind, folder):
            r = conn.execute("SELECT * FROM media_files WHERE path=?", (str(p),)).fetchone()
            pend = conn.execute("SELECT 1 FROM pending_deletes WHERE path=?", (str(p),)).fetchone()
            out.append({"name": p.name, "size": p.stat().st_size, "duration": r["duration"] if r else None,
                        "bitrate": r["bitrate_kbps"] if r else None, "thumb": bool(r and r["thumb"]),
                        "added_at": r["added_at"] if r else p.stat().st_mtime,
                        "status": "delete_pending" if pend else "ok"})
        pend_dir = paths.incoming / kind / folder
        pending = sorted(p.name for p in pend_dir.iterdir() if p.is_file()) if pend_dir.is_dir() else []
        return {"files": out, "pending": pending}

    @app.post("/api/media/{kind}")
    async def folder_create(request: Request, kind: str):
        name = str((await request.json()).get("name", "")).strip()
        if not _kind(kind) or not config.valid_folder_name(name):
            return err("폴더 이름은 문자/숫자로 시작하고 문자·숫자·공백·._-() 만 쓸 수 있습니다")
        (paths.kind_root(kind) / name).mkdir(parents=True, exist_ok=True)
        (paths.incoming / kind / name).mkdir(parents=True, exist_ok=True)
        return {"ok": True}

    @app.put("/api/media/{kind}/{folder}")
    async def folder_rename(request: Request, kind: str, folder: str):
        new = str((await request.json()).get("name", "")).strip()
        if not _kind(kind) or not config.valid_folder_name(folder) or not config.valid_folder_name(new):
            return err("폴더 이름 오류")
        used = store.folder_usage(kind).get(folder)
        if used:
            return err(f"{', '.join(used)} 에서 사용 중 — 라이브 설정에서 먼저 빼세요")
        src, dst = paths.kind_root(kind) / folder, paths.kind_root(kind) / new
        if dst.exists():
            return err("같은 이름의 폴더가 있습니다")
        os.rename(src, dst)
        for base in (paths.incoming / kind, paths.thumbs_dir / kind):
            if (base / folder).is_dir():
                os.rename(base / folder, base / new)
        conn = request.state.conn
        for r in conn.execute("SELECT path FROM media_files WHERE kind=? AND folder=?", (kind, folder)).fetchall():
            conn.execute("UPDATE media_files SET path=?, folder=?, thumb=REPLACE(thumb, ?, ?) WHERE path=?",
                         (str(dst / Path(r["path"]).name), new, f"{kind}/{folder}/", f"{kind}/{new}/", r["path"]))
        return {"ok": True}

    @app.delete("/api/media/{kind}/{folder}")
    def folder_delete(kind: str, folder: str):
        if not _kind(kind) or not config.valid_folder_name(folder):
            return err("폴더 이름 오류")
        used = store.folder_usage(kind).get(folder)
        if used:
            return err(f"{', '.join(used)} 에서 사용 중이라 삭제할 수 없습니다")
        if catalog.list_files(paths, kind, folder):
            return err("폴더가 비어 있지 않습니다. 파일을 먼저 삭제하세요")
        shutil.rmtree(paths.kind_root(kind) / folder, ignore_errors=True)
        shutil.rmtree(paths.incoming / kind / folder, ignore_errors=True)
        return {"ok": True}

    @app.delete("/api/media/{kind}/{folder}/{name}")
    def file_delete(request: Request, kind: str, folder: str, name: str):
        if not _kind(kind) or not config.valid_folder_name(folder) or "/" in name or name.startswith("."):
            return err("잘못된 요청")
        p = paths.kind_root(kind) / folder / name
        if not p.is_file():
            return err("파일 없음", 404)
        conn = request.state.conn
        if kind == "video" and str(p) in _referenced_by_concat(paths):
            conn.execute("INSERT OR IGNORE INTO pending_deletes(path, requested_at) VALUES (?,?)", (str(p), time.time()))
            return {"ok": True, "deferred": True,
                    "message": "지금 송출 목록에 들어 있어 '삭제 예약'했습니다. 다음 재시작 뒤 자동 삭제됩니다"}
        p.unlink()
        catalog.remove(conn, p)
        if kind == "video":
            thumbs.thumb_path(paths, folder, name).unlink(missing_ok=True)
        return {"ok": True}

    @app.get("/api/thumbs/{folder}/{name}")
    def thumb(folder: str, name: str):
        if not config.valid_folder_name(folder) or "/" in name or name.startswith("."):
            return err("bad", 400)
        p = paths.thumbs_dir / "video" / folder / f"{name}.jpg"
        if not p.is_file():
            return err("not found", 404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ------------------------------------------------------------------ 이어 올리기 업로드
    def _upload_id(kind: str, folder: str, name: str, size: int) -> str:
        return hashlib.sha1(f"{kind}/{folder}/{name}/{size}".encode()).hexdigest()[:24]

    @app.post("/api/uploads")
    async def upload_begin(request: Request):
        b = await request.json()
        kind, folder, name, size = str(b.get("kind")), str(b.get("folder")), Path(str(b.get("name", ""))).name, int(b.get("size", 0))
        if not _kind(kind) or not config.valid_folder_name(folder) or not (paths.kind_root(kind) / folder).is_dir():
            return err("폴더 없음")
        if not name or name.startswith(".") or "#{" in name or not name.lower().endswith(EXTS[kind]):
            return err(f"허용 확장자: {', '.join(EXTS[kind])}")
        if size <= 0 or size > int(settings["dashboard"]["max_upload_mb"]) * 1024 * 1024:
            return err("파일 크기 오류")
        uid = _upload_id(kind, folder, name, size)
        meta = paths.uploads / f"{uid}.json"
        part = paths.uploads / f"{uid}.part"
        if not meta.exists():
            meta.write_text(json.dumps({"kind": kind, "folder": folder, "name": name, "size": size}))
            part.touch()
        return {"id": uid, "offset": part.stat().st_size if part.exists() else 0}

    @app.put("/api/uploads/{uid}")
    async def upload_chunk(request: Request, uid: str, offset: int):
        if not uid.isalnum():
            return err("bad id")
        part = paths.uploads / f"{uid}.part"
        meta = paths.uploads / f"{uid}.json"
        if not meta.exists():
            return err("업로드 정보 없음", 404)
        cur = part.stat().st_size if part.exists() else 0
        if offset != cur:
            return JSONResponse({"ok": False, "offset": cur}, status_code=409)
        size = json.loads(meta.read_text())["size"]
        with open(part, "ab") as f:
            async for chunk in request.stream():
                f.write(chunk)
                if f.tell() > size:
                    break
        new = part.stat().st_size
        if new > size:
            part.unlink(); meta.unlink()
            return err("크기 초과 — 다시 올려 주세요")
        return {"ok": True, "offset": new}

    @app.post("/api/uploads/{uid}/complete")
    def upload_complete(uid: str):
        if not uid.isalnum():
            return err("bad id")
        meta, part = paths.uploads / f"{uid}.json", paths.uploads / f"{uid}.part"
        if not meta.exists() or not part.exists():
            return err("업로드 정보 없음", 404)
        m = json.loads(meta.read_text())
        if part.stat().st_size != m["size"]:
            return err("아직 다 올라가지 않았습니다")
        dest_dir = paths.incoming / m["kind"] / m["folder"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / m["name"]
        if dest.exists():
            dest = dest_dir / f"{Path(m['name']).stem}-{uid[:6]}{Path(m['name']).suffix}"
        os.rename(part, dest)  # 완성된 파일만 스테이징에 나타난다 → ingest 가 검증 후 방송 폴더로
        meta.unlink()
        return {"ok": True, "message": "업로드 완료 — 규격 검증 중"}

    # ------------------------------------------------------------------ 시스템 / 설정
    @app.get("/api/system")
    def system(request: Request):
        conn = request.state.conn
        disk = []
        for label, p in (("영상", paths.video), ("음원", paths.audio), ("업로드 대기", paths.incoming),
                         ("격리", paths.rejected), ("로그", paths.logs), ("상태·DB", paths.state)):
            disk.append({"label": label, "bytes": sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0})
        sess = request.state.session
        return {
            "version": core.version, "uptime_since": core.started_at, "disk": disk,
            "reboot_required": REBOOT_FLAG.exists(),
            "reboot_packages": REBOOT_FLAG.with_suffix(".pkgs").read_text().split() if REBOOT_FLAG.with_suffix(".pkgs").exists() else [],
            "settings": {"restart_base_utc": settings["restart"]["base_utc"],
                         "tls_relay": settings["output"]["tls_relay"]["enabled"]},
            "sessions": [{"id": r["token_hash"], "ip": r["ip"], "ua": r["user_agent"], "last_seen": r["last_seen"],
                          "current": r["token_hash"] == sess["token_hash"]}
                         for r in conn.execute("SELECT * FROM sessions ORDER BY last_seen DESC").fetchall()],
            "attempts": [dict(r) for r in conn.execute("SELECT ts, ip, username, success FROM login_attempts ORDER BY ts DESC LIMIT 30").fetchall()],
            "update_requested": (paths.state / "update.request").exists(),
        }

    @app.put("/api/system/settings")
    async def system_settings(request: Request):
        b = await request.json()
        if "restart_base_utc" in b:
            try:
                config.hhmm_to_minutes(b["restart_base_utc"])
            except ConfigError as e:
                return err(str(e))
            config.save_settings_override(paths, ["restart", "base_utc"], b["restart_base_utc"])
            settings["restart"]["base_utc"] = b["restart_base_utc"]
        if "tls_relay" in b:
            config.save_settings_override(paths, ["output", "tls_relay", "enabled"], bool(b["tls_relay"]))
            settings["output"]["tls_relay"]["enabled"] = bool(b["tls_relay"])
        return {"ok": True}

    @app.post("/api/system/cleanup")
    def system_cleanup():
        return {"ok": core.cleanup_async(manual=True), "message": "정리를 시작했습니다 (결과는 경고/이벤트에 기록)"}

    @app.post("/api/system/update")
    def system_update():
        # 호스트의 livemax-updater 가 이 파일을 보고 새 이미지로 교체한다 (컨테이너는 자기 자신을 교체할 수 없음)
        (paths.state / "update.request").write_text(str(time.time()))
        return {"ok": True, "message": "업데이트를 요청했습니다. 1~2분 뒤 자동으로 새 버전으로 바뀝니다 (모든 라이브가 잠깐 끊겼다가 순서대로 복귀)"}

    @app.delete("/api/system/sessions/{sid}")
    def session_revoke(request: Request, sid: str):
        auth.revoke(request.state.conn, sid)
        return {"ok": True}

    @app.get("/api/system/export")
    def export():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for lv in store.all():  # 스트림 키는 절대 포함하지 않는다
                z.writestr(f"lives/{lv.id}.yaml", yaml.safe_dump(lv.to_dict(), allow_unicode=True, sort_keys=False))
            if paths.settings_file.exists():
                z.writestr("settings.yaml", paths.settings_file.read_text(encoding="utf-8"))
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="livemax-config-{time.strftime("%Y%m%d")}.zip"'})

    @app.post("/api/system/import")
    async def import_(request: Request):
        raw = await request.body()
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            return err("zip 파일이 아닙니다")
        added = []
        for n in z.namelist():
            if not (n.startswith("lives/") and n.endswith(".yaml")):
                continue
            src = live_from_dict(yaml.safe_load(z.read(n)))
            lv = store.new(src.name)  # 새 서버 기준으로 ID·포트·슬롯 재배정
            for k in ("video_folders", "audio_folders", "audio_mode", "norepeat_count", "crossfade", "bitrate"):
                setattr(lv, k, getattr(src, k))
            lv.enabled = False  # 키가 없으므로 비활성으로 가져온다
            try:
                store.save(lv)
                added.append(lv.id)
            except ConfigError as e:
                return err(f"{n}: {e}")
        return {"ok": True, "added": added, "message": f"{len(added)}개 라이브를 가져왔습니다. 각 라이브에 스트림 키를 넣고 시작하세요"}

    # ------------------------------------------------------------------ 화면
    app.mount("/assets", StaticFiles(directory=str(STATIC)), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
