"""웹 API + 화면(SPA) — 브라우저에서 모든 운영을 한다 (v2).

외부 접속은 Caddy(HTTPS) → 127.0.0.1:8080. 시각은 전부 epoch 초로 내보내고 브라우저가 현지 시간으로 표시한다.
보안: argon2 비밀번호, IP별 5회 실패 15분 차단, HttpOnly+Secure+SameSite=Strict 세션, 같은 출처만 변경 허용.
스트림 키는 응답 어디에도 평문으로 나가지 않는다 (****1a2b).
"""

from __future__ import annotations

import asyncio

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
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import capacity, catalog, channels, config, db, filenames, liquidsoap, notify, secrets, thumbs
from ..config import ConfigError, Live, live_from_dict
from ..state import read_json
from . import auth

log = logging.getLogger("livemax.web")
STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "::1"}
EXTS = filenames.EXTS  # 파일 이름 규칙은 한 곳(filenames.py)에서


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
    chstore = channels.ChannelStore(paths)
    notifier = getattr(core, "notifier", None) or notify.Notifier(paths, settings)
    core.notifier = notifier  # 텔레그램 명령 답장(notifier.answer)을 어디서든 같은 객체로
    # 호스트의 /var/run/reboot-required 를 livemax tick(호스트 타이머)이 data/state/host 로 복사해 준다
    REBOOT_FLAG = paths.state / "host" / "reboot-required"
    app = FastAPI(title="LiveMax", docs_url=None, redoc_url=None, openapi_url=None)
    initial = auth.ensure_initial_password()
    if initial:
        log.warning("관리자 초기 비밀번호를 생성했습니다: %s", auth.initial_file())

    # ------------------------------------------------------------------ 미들웨어
    # 요청 처리 루프(이벤트 루프)는 DB·디스크를 절대 직접 기다리지 않는다.
    # v2.3 까지는 여기서 DB 를 바로 열고 세션을 갱신했는데, 정리·지표 저장이 DB 를 잠깐 잠그면 그동안
    # 화면 파일(app.js)까지 모든 요청이 멈췄다 (실측: 쓰기 잠금 6초 → 모든 응답 5초+).
    def _pre(request: Request, path: str):
        conn = db.connect(paths.db)
        request.state.conn = conn
        request.state.ip = client_ip(request)
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
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            resp = await call_next(request)
            resp.headers["X-Frame-Options"] = "DENY"
            return resp
        t0 = time.monotonic()
        try:
            denied = await run_in_threadpool(_pre, request, path)
            if denied is not None:
                return denied
            if request.method != "GET":
                _ov_cache["data"] = None  # 시작·정지·설정 변경 직후엔 새로 계산
            resp = await call_next(request)
            if "cache-control" not in resp.headers:  # 썸네일처럼 직접 캐시를 정한 응답은 그대로 (매번 다시 받지 않게)
                resp.headers["Cache-Control"] = "no-store"
            return resp
        finally:
            conn = getattr(request.state, "conn", None)
            if conn is not None:
                await run_in_threadpool(conn.close)
            took = time.monotonic() - t0
            if took > 3 and not path.startswith("/api/uploads"):
                log.warning("느린 요청 %.1fs %s %s", took, request.method, path)

    # ------------------------------------------------------------------ 로그인
    @app.get("/api/session")
    def session(request: Request):
        sess = auth.get_session(request.state.conn, request.cookies.get(auth.COOKIE))
        return {"authenticated": bool(sess), "must_change": auth.must_change(), "version": core.version,
                "username": settings["dashboard"]["username"], "server_name": config.server_name(settings),
                "lives_view": settings.get("ui", {}).get("lives_view", "grid")}

    @app.post("/api/login")
    def login(request: Request, body: dict = Body(default={})):
        conn, ip, d = request.state.conn, request.state.ip, settings["dashboard"]
        remain = auth.blocked_seconds(conn, ip, int(d["login_max_failures"]), int(d["login_block_minutes"]))
        if remain:
            return err(f"로그인 실패가 많아 {remain // 60 + 1}분간 차단되었습니다", 429)
        user, pw = str(body.get("username", "")), str(body.get("password", ""))
        ok = user == d["username"] and auth.verify_password(pw)
        auth.record_attempt(conn, ip, user, ok)
        if not ok:
            if auth.blocked_seconds(conn, ip, int(d["login_max_failures"]), int(d["login_block_minutes"])):
                notifier.send("security", "로그인 차단", f"IP {ip} 에서 비밀번호를 {d['login_max_failures']}번 틀려 "
                                                       f"{d['login_block_minutes']}분간 차단했습니다")
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
    def password(request: Request, b: dict = Body(default={})):
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
        tinfo = sup.timer_info(lv)
        desired = bool(s.get("desired"))
        if tinfo["next_start_at"] and not desired:
            kind = "waiting"  # 타이머 반복 — 다음 방송 대기
        elif not desired:
            kind = "stopped"  # 송출하지 않는 중 — 화면에는 '대기중' (문제 아님)
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
        key_path = channels.live_key_path(paths, lv)
        key_masked = secrets.mask(key_path.read_text().strip()) if key_path.exists() else ""
        return {
            **lv.to_dict(), "status": kind, "state": state, "message": s.get("message", ""),
            "speed": speed, "bitrate_kbps": s.get("bitrate"), "out_time": s.get("out_time"),
            "started_at": s.get("ffmpeg_started_at") if state == "running" else None,
            "transport": s.get("transport"), "clip": s.get("clip"), "clip_folder": s.get("clip_folder"),
            "clip_v": _thumb_v(s.get("clip_folder"), s.get("clip")),
            "planned": bool(s.get("planned")), "starting": bool(s.get("starting")),
            "song_title": np_.get("title") or (Path(np_.get("file", "")).stem if np_.get("file") else ""),
            "song_artist": np_.get("artist") or "",
            "song_folder": Path(np_["file"]).parent.name if np_.get("file") else "", "restarts_hour": s.get("restarts_hour", 0), "blips_day": s.get("blips_day"),
            "restarts": s.get("restarts", []), "next_restart": lv.next_restart(settings).timestamp(),
            "key_masked": key_masked, "key_label": _key_label(lv), "desired": desired,
            "idle_thumb": None if state == "running" else _idle_thumb(lv),
            "cpu": (core.collector.per_live.get(lv.id) or {}).get("cpu"),
            "rss_mb": (core.collector.per_live.get(lv.id) or {}).get("rss_mb"),
            **tinfo,
        }

    def _thumb_v(folder: str | None, name: str | None) -> int:
        """썸네일 그림의 판(수정 시각) — 주소에 붙여 그림이 바뀌면 크롬이 옛 그림을 못 쓰게."""
        if not folder or not name:
            return 0
        try:
            return thumbs.thumb_path(paths, folder, name).stat().st_mtime_ns // 1_000_000
        except OSError:
            return 0

    def _idle_thumb(lv: Live) -> dict | None:
        """송출 전에도 어떤 비디오인지 알 수 있게: 고른 비디오 폴더 중 썸네일이 있는 첫 클립 (숫자 순)."""
        for folder in lv.video_folders:
            for p in catalog.list_files(paths, "video", folder):
                v = _thumb_v(folder, p.name)
                if v:
                    return {"folder": folder, "name": p.name, "v": v}
        return None

    def _key_label(lv: Live) -> str:
        ch = chstore.get(lv.channel_id) if lv.channel_id else None
        k = ch.key(lv.key_id) if ch else None
        return k["label"] if k else ""

    def _video_kbps(conn, lv: dict) -> float:
        """라이브 1개의 영상 비트레이트: 송출 중이면 실측(전체 - 오디오), 아니면 고른 비디오 폴더의 평균."""
        audio = float(settings["capacity"]["audio_kbps"])
        if lv["state"] == "running" and lv.get("bitrate_kbps") and (lv.get("out_time") or 0) > 30:
            return max(500.0, float(lv["bitrate_kbps"]) - audio)
        if lv["video_folders"]:  # 아직 송출 전이면 고른 비디오 파일들의 평균
            q = ",".join("?" * len(lv["video_folders"]))
            r = conn.execute(f"SELECT SUM(bitrate_kbps*duration)/SUM(duration) AS k FROM media_files "
                             f"WHERE kind='video' AND folder IN ({q}) AND bitrate_kbps>0 AND duration>0",
                             lv["video_folders"]).fetchone()
            if r and r["k"]:
                return float(r["k"])
        return 5000.0

    _MEDIA_TOT: dict = {"t": None, "v": {}}  # t=None: 아직 한 번도 안 셈 (켜진 지 1분 안 된 서버에서도 바로 세게)

    def _media_totals(conn) -> dict:
        """서버 박스 디스크 아래 '현재 비디오 N개 (용량)·음악 N개' — 1분 동안 기억 (5초마다 DB 를 훑지 않게)."""
        now = time.monotonic()
        if _MEDIA_TOT["t"] is None or now - _MEDIA_TOT["t"] > 60:
            v = {k: {"n": 0, "bytes": 0} for k in ("video", "audio")}
            for r in conn.execute("SELECT kind, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM media_files GROUP BY kind"):
                if r["kind"] in v:
                    v[r["kind"]] = {"n": r["n"], "bytes": r["b"]}
            _MEDIA_TOT.update(t=now, v=v)
        return _MEDIA_TOT["v"]

    def overview_data(conn) -> dict:
        snap = sup.snapshot()
        lives = [live_view(lv, snap) for lv in store.all()]
        sysd = dict(core.collector.latest)
        planned = [lv for lv in lives if lv["enabled"]]  # 송출 중이거나 켜 둔 라이브만 (대기중은 제외)
        on_air = [lv for lv in planned if lv["state"] == "running"]
        cap = capacity.compute(settings["capacity"], [_video_kbps(conn, lv) for lv in planned],
                               sysd.get("month_tx_bytes"), sysd.get("net_tx_mbps"), sysd.get("cpu_pct"),
                               sysd.get("mem_used_mb"), sysd.get("mem_total_mb"),
                               live_cpu=[lv["cpu"] for lv in on_air if lv.get("cpu")], running=len(on_air))
        # 안전장치: 설정한 최대 개수까지 남은 칸보다 많이 '추가 가능'으로 보이지 않게
        left = max(0, _max_lives() - len(lives))
        if left < cap.can_add:
            cap.can_add, cap.limiting = left, "max"
            cap.usage_ratio = len(lives) / max(1, _max_lives())
        alerts = []
        for lv in lives:
            if lv["status"] in ("warn", "down"):
                alerts.append({"level": "warn" if lv["status"] == "warn" else "error", "ts": time.time(),
                               "live": lv["id"], "message": lv["message"] or ("speed 저하" if lv["status"] == "warn" else "중단")})
        if sysd.get("disk_pct") and sysd["disk_pct"] >= settings["cleanup"]["disk_warn_percent"]:
            alerts.append({"level": "warn", "ts": time.time(), "live": None, "message": f"디스크 {sysd['disk_pct']:.0f}% 사용 중"})
        if cap.limit_tb > 0 and cap.projected_tb > cap.limit_tb:
            alerts.append({"level": "error", "ts": time.time(), "live": None,
                           "message": f"이번 달 트래픽이 {cap.projected_tb:.1f}TB 로 예상돼 한계선 {cap.limit_tb:.0f}TB 를 넘습니다 — 라이브를 줄이세요"})
        if REBOOT_FLAG.exists():
            alerts.append({"level": "warn", "ts": REBOOT_FLAG.stat().st_mtime, "live": None,
                           "message": "서버 재부팅 필요 (보안 업데이트 적용 대기) — 설정 > 시스템"})
        for e in conn.execute("SELECT * FROM events WHERE level IN ('warn','error') AND ts > ? ORDER BY ts DESC LIMIT 20",
                              (time.time() - 86400,)).fetchall():
            alerts.append({"id": e["id"], "level": e["level"], "ts": e["ts"], "live": e["live_id"], "message": e["message"]})
        return {
            "server_time": time.time(), "version": core.version, "uptime_since": core.started_at,
            "lives": lives, "system": sysd, "channels": [_channel_brief(c) for c in chstore.all()],
            "capacity": {"can_add": cap.can_add, "limiting": cap.limiting, "projected_tb": cap.projected_tb,
                         "limit_tb": cap.limit_tb, "target_tb": cap.target_tb, "usage_ratio": cap.usage_ratio, "month_tx_tb": cap.month_tx_tb,
                         "per_live_tb": cap.per_live_tb, "per_live_mbps": cap.per_live_mbps, "lives": len(planned),
                         "mem_per_live_mb": cap.mem_per_live_mb, "cpu_per_live_pct": cap.cpu_per_live_pct,
                         "port_mbit": cap.port_mbit, "traffic_unlimited": cap.traffic_unlimited,
                         "mem_reserve_mb": settings["capacity"].get("mem_reserve_mb", 700),
                         "cpu_target_pct": settings["capacity"].get("cpu_target_pct", 50),
                         "port_use_ratio": settings["capacity"].get("port_use_ratio", 0.8),
                         "disk_warn_pct": settings["capacity"].get("disk_warn_pct", 85),
                         "options": cap.options, "usage": cap.usage},
            "max_lives": _max_lives(),
            "day_start": config.day_start(settings),  # 화면의 '(1일)' 숫자 기준 — 일일 재시작 시각(기본 한국 자정)
            "media": _media_totals(conn),
            "alerts": alerts[:30], "reboot_required": REBOOT_FLAG.exists(),
        }

    def _channel_brief(c) -> dict:
        return {"id": c.id, "name": c.name, "description": c.description, "url": c.url,
                "logo": f"/api/channels/{c.id}/logo?v={c.logo_ver}" if c.logo else ""}

    # ------------------------------------------------------------------ 채널
    def _channel_full(c, lives, snap) -> dict:
        users = channels.key_users(lives)
        keys = []
        for k in sorted(c.keys, key=lambda k: catalog.natural_key(k["label"])):  # 이름순 1,2,…,10 · 가나다 (만든 순서 X)
            lv = users.get((c.id, k["id"]))
            live_now = bool(lv and snap.get(lv.id, {}).get("desired"))
            keys.append({"id": k["id"], "label": k["label"], "masked": chstore.masked(c.id, k["id"]),
                         "used_by": lv.id if lv else None, "used_by_name": lv.name if lv else None,
                         "status": "live" if live_now else ("registered" if lv else "free")})
        return {**_channel_brief(c), "keys": keys, "lives": [lv.id for lv in lives if lv.channel_id == c.id]}

    @app.get("/api/channels")
    def channels_list():
        lives, snap = store.all(), sup.snapshot()
        return {"channels": [_channel_full(c, lives, snap) for c in chstore.all()]}

    def _channel_fields(c, b: dict):
        c.name = str(b.get("name", c.name) or "").strip()[:40]
        c.description = str(b.get("description", c.description) or "").strip()[:120]
        c.url = str(b.get("url", c.url) or "").strip()[:300]

    @app.post("/api/channels")
    def channel_create(request: Request, b: dict = Body(default={})):
        c = chstore.new(str(b.get("name", "")))
        try:
            _channel_fields(c, b)
            chstore.save(c)
        except ConfigError as e:
            return err(str(e))
        db.add_event(request.state.conn, "info", "web", f"채널 추가: {c.name}")
        return {"ok": True, "id": c.id}

    @app.put("/api/channels/{cid}")
    def channel_update(cid: str, b: dict = Body(default={})):
        c = chstore.get(cid)
        if not c:
            return err("채널 없음", 404)
        try:
            _channel_fields(c, b)
            chstore.save(c)
        except ConfigError as e:
            return err(str(e))
        return {"ok": True}

    @app.delete("/api/channels/{cid}")
    def channel_delete(request: Request, cid: str):
        c = chstore.get(cid)
        if not c:
            return err("채널 없음", 404)
        used = [lv.name for lv in store.all() if lv.channel_id == cid]
        if used:
            return err(f"이 채널의 라이브({', '.join(used)})를 먼저 지우거나 다른 채널로 옮기세요")
        chstore.delete(cid)
        db.add_event(request.state.conn, "info", "web", f"채널 삭제: {c.name} (스트림 키 {len(c.keys)}개 함께 삭제)")
        return {"ok": True}

    @app.post("/api/channels/{cid}/keys")
    def channel_key_add(request: Request, cid: str, b: dict = Body(default={})):
        c = chstore.get(cid)
        if not c:
            return err("채널 없음", 404)
        try:
            kid = chstore.add_key(c, str(b.get("label", "")), str(b.get("key", "")))
        except ConfigError as e:
            return err(str(e))
        db.add_event(request.state.conn, "info", "web", f"채널 '{c.name}' 스트림 키 추가: {b.get('label', '')}")
        return {"ok": True, "id": kid}

    @app.delete("/api/channels/{cid}/keys/{kid}")
    def channel_key_delete(request: Request, cid: str, kid: str):
        c = chstore.get(cid)
        if not c or not c.key(kid):
            return err("키 없음", 404)
        lv = channels.key_users(store.all()).get((cid, kid))
        if lv:
            return err(f"'{lv.name}' 라이브가 쓰는 키라 지울 수 없습니다 — 라이브에서 다른 키로 바꾸거나 라이브를 지우세요")
        chstore.remove_key(c, kid)
        db.add_event(request.state.conn, "info", "web", f"채널 '{c.name}' 스트림 키 삭제")
        return {"ok": True}

    @app.post("/api/channels/{cid}/logo")
    async def channel_logo_upload(cid: str, request: Request):
        data = await request.body()
        return await run_in_threadpool(_save_logo, cid, data)

    def _save_logo(cid: str, data: bytes):
        c = chstore.get(cid)
        if not c:
            return err("채널 없음", 404)
        if len(data) > channels.LOGO_MAX:
            return err("로고는 1MB 이하로 올려 주세요")
        t = channels.logo_type(data)
        if not t:
            return err("로고는 PNG, JPG, WEBP 이미지만 됩니다")
        p = chstore.logo_path(cid)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        c.logo, c.logo_ver = t, c.logo_ver + 1
        chstore.save(c)
        return {"ok": True}

    @app.delete("/api/channels/{cid}/logo")
    def channel_logo_delete(cid: str):
        c = chstore.get(cid)
        if not c:
            return err("채널 없음", 404)
        chstore.logo_path(cid).unlink(missing_ok=True)
        c.logo, c.logo_ver = "", c.logo_ver + 1
        chstore.save(c)
        return {"ok": True}

    @app.get("/api/channels/{cid}/logo")
    def channel_logo(cid: str):
        c = chstore.get(cid)
        p = chstore.logo_path(cid) if c else None
        if not c or not c.logo or not p.exists():
            return err("not found", 404)
        return FileResponse(p, media_type=channels.LOGO_TYPES[c.logo],
                            headers={"Cache-Control": "private, max-age=2592000", "X-Content-Type-Options": "nosniff"})

    _ov_cache = {"t": 0.0, "data": None}

    def _max_lives() -> int:
        return int(settings.get("max_lives") or 40)

    # ------------------------------------------------------------------ 텔레그램 명령 (읽기 전용)
    _NOT_DROP = ("일일 재시작", "설정 적용", "사용자 재시작", "유튜브 짧은 재연결")

    def _tg_answer(text: str) -> str | None:
        """텔레그램 대화방 명령 → 답장. /상태 /오늘 /라이브 N /도움말 (영어 /status /today /live N 도 됨)."""
        t = text.strip().lstrip("/").split("@")[0]
        cmd, _, arg = t.partition(" ")
        cmd = cmd.lower()
        with db.session(paths.db) as c:
            ov = overview_data(c)
        srv = notifier.label()
        lives, ds = ov["lives"], ov.get("day_start", 0)
        chn = {ch["id"]: ch["name"] for ch in ov.get("channels", [])}
        nm = lambda lv: f"{lv['name']}" + (f" ({chn[lv['channel_id']]})" if chn.get(lv.get("channel_id")) else "")

        def drops(lv):
            st = max(float(lv.get("session_started_at") or 0), ds)
            return [r for r in lv.get("restarts", []) if r["ts"] >= st and not str(r["reason"]).startswith(_NOT_DROP)]

        if cmd in ("상태", "status"):
            act = [lv for lv in lives if lv.get("desired")]
            ok = sum(lv["status"] == "ok" for lv in act)
            warn = sum(lv["status"] == "warn" for lv in act)
            down = sum(lv["status"] == "down" for lv in act)
            pct = f"{ok * 100 // len(act)}%" if act else "-"
            sysd, cap = ov.get("system") or {}, ov.get("capacity") or {}
            mem = f"{(sysd.get('mem_used_mb') or 0) / 1024:.1f} / {(sysd.get('mem_total_mb') or 0) / 1024:.1f}GB" if sysd.get("mem_total_mb") else "-"
            bad = [f"  ⚠ {nm(lv)} — {'중단' if lv['status'] == 'down' else '경고'}" for lv in act if lv["status"] in ("warn", "down")]
            return "\n".join([f"[{srv}] 가동률 {pct}",
                              f"라이브 {len(act)} · 정상 {ok} · 경고 {warn} · 중단 {down} · 끊김 {sum(len(drops(lv)) for lv in act)} (1일)",
                              f"CPU {sysd.get('cpu_pct', 0):.0f}% · 메모리 {mem} · 회선 {sysd.get('net_tx_mbps', 0):.0f}Mbps",
                              f"추가 가능 {cap.get('can_add', '-')}개"] + bad)
        if cmd in ("오늘", "today"):
            rows = []
            for lv in lives:
                d, b = drops(lv), lv.get("blips_day") or {}
                if d or b.get("n"):
                    rows.append(f"• {nm(lv)}: 끊김 {len(d)} · 밀림 {b.get('n', 0)}회" + (f" (최대 {b.get('max')}초)" if b.get("n") else ""))
            return f"[{srv}] 오늘(자정부터)\n" + ("\n".join(rows) if rows else "문제 없음 ✅")
        if cmd in ("라이브", "live"):
            q = arg.strip().lower()
            hit = [lv for lv in lives if q and (lv["name"].lower() == q or lv["name"].lower() == f"live {q}" or lv["id"] == q)]
            if not hit:
                return "그 라이브를 못 찾았습니다 — 예: /라이브 18"
            lv = hit[0]
            label = {"ok": "정상", "warn": "경고", "down": "중단", "restarting": "재시작중", "stopped": "대기중", "waiting": "다음 방송 대기"}
            out = [f"[{srv}] {nm(lv)} — {label.get(lv['status'], lv['status'])}"]
            if lv.get("state") == "running":
                out.append(f"{(lv.get('bitrate_kbps') or 0):,.0f}kbps · speed {lv.get('speed') or 0:.2f}x")
            if lv.get("song_title"):
                out.append(f"♪ {lv['song_title']}")
            b = lv.get("blips_day") or {}
            out.append(f"오늘 끊김 {len(drops(lv))} · 밀림 {b.get('n', 0)}회")
            if lv.get("message"):
                out.append(lv["message"])
            return "\n".join(out)
        return (f"[{srv}] LiveMax 명령\n/상태 — 전체 요약\n/오늘 — 오늘 끊김·밀림이 있었던 라이브\n"
                "/라이브 18 — 라이브 하나 자세히\n(읽기 전용 — 조작은 사이트에서)")

    notifier.answer = _tg_answer

    @app.get("/api/overview")
    def overview(request: Request):
        now = time.monotonic()
        if _ov_cache["data"] is not None and now - _ov_cache["t"] < 4:
            return _ov_cache["data"]
        data = overview_data(request.state.conn)
        _ov_cache.update(t=time.monotonic(), data=data)
        return data

    # 알림 목록에서 확인한 항목 지우기 (지금 상태를 나타내는 알림 — 중단·디스크·재부팅 — 은 해결되면 저절로 사라짐)
    @app.delete("/api/alerts/{eid}")
    def alert_dismiss(request: Request, eid: int):
        request.state.conn.execute("DELETE FROM events WHERE id=? AND level IN ('warn','error')", (eid,))
        return {"ok": True}

    @app.delete("/api/alerts")
    def alerts_clear(request: Request):
        request.state.conn.execute("DELETE FROM events WHERE level IN ('warn','error')")
        return {"ok": True}

    # ------------------------------------------------------------------ 라이브
    def _live_payload(b: dict, lv: Live) -> tuple[Live, str, str]:
        lv.name = str(b.get("name", lv.name)).strip()[:80]
        lv.video_folders = [str(x) for x in b.get("video_folders", lv.video_folders)]
        lv.audio_folders = [str(x) for x in b.get("audio_folders", lv.audio_folders)]
        lv.audio_mode = str(b.get("audio_mode", lv.audio_mode))
        lv.norepeat_count = int(b.get("norepeat_count", lv.norepeat_count))
        lv.crossfade = float(b.get("crossfade", lv.crossfade) or 0)
        lv.fast_relay = bool(b.get("fast_relay", lv.fast_relay))
        if b.get("video_order") in ("sequential", "random"):
            lv.video_order = b["video_order"]
        lv.timer_mode = str(b.get("timer_mode", lv.timer_mode))
        lv.timer_minutes = int(b.get("timer_minutes", lv.timer_minutes))
        lv.timer_until = float(b.get("timer_until", lv.timer_until) or 0)
        lv.timer_repeat = bool(b.get("timer_repeat", lv.timer_repeat))
        lv.timer_gap_minutes = int(b.get("timer_gap_minutes", lv.timer_gap_minutes))
        lv.youtube_video_id = str(b.get("youtube_video_id", lv.youtube_video_id) or "").strip()
        if "channel_id" in b or "key_id" in b:
            cid, kid = str(b.get("channel_id") or ""), str(b.get("key_id") or "")
            ch = chstore.get(cid) if cid else None
            if cid and not ch:
                raise ConfigError("채널이 없습니다")
            if kid and not (ch and ch.key(kid) and chstore.key_path(cid, kid).exists()):
                raise ConfigError("그 채널에 등록된 스트림 키가 아닙니다")
            lv.channel_id, lv.key_id = cid, kid
        return lv, str(b.get("apply", "now"))

    @app.post("/api/lives")
    def live_create(request: Request, b: dict = Body(default={})):
        if len(store.all()) >= _max_lives():
            return err(f"이 서버의 라이브 최대 개수({_max_lives()}개)에 도달했습니다 — 설정 > 서버에서 늘리거나 안 쓰는 라이브를 지우세요")
        lv = store.new(str(b.get("name", "")).strip()[:80])
        try:
            lv, apply = _live_payload(b, lv)
            if not lv.key_id:
                return err("채널과 스트림 키를 고르세요 (키는 채널 메뉴에서 등록)")
            lv.enabled = False  # 새 라이브는 '대기중' — 목록에서 시작을 눌러야 송출 (재부팅 때 저절로 시작되지 않게)
            store.save(lv)
        except (ConfigError, ValueError) as e:
            return err(str(e))
        db.add_event(request.state.conn, "info", "web", f"라이브 생성: {lv.name}", lv.id)
        if apply == "now":
            try:
                sup.start(lv.id)
            except ConfigError as e:
                return {"ok": True, "id": lv.id, "message": f"저장했지만 시작하지 못했습니다: {e}"}
        return {"ok": True, "id": lv.id}

    @app.put("/api/lives/{live_id}")
    def live_update(request: Request, live_id: str, b: dict = Body(default={})):
        lv = store.get(live_id)
        if not lv:
            return err("라이브 없음", 404)
        before = lv.to_dict()
        try:
            lv, apply = _live_payload(b, lv)
            store.save(lv)
        except (ConfigError, ValueError) as e:
            return err(str(e))
        after = lv.to_dict()
        key = (before["channel_id"], before["key_id"]) != (after["channel_id"], after["key_id"])
        if key and not after["key_id"]:
            return err("스트림 키를 고르세요")
        db.add_event(request.state.conn, "info", "web", "설정 변경" + (" (스트림 키 교체)" if key else ""), lv.id)
        audio_changed = any(before[k] != after[k] for k in ("audio_mode", "norepeat_count", "crossfade"))
        video_changed = (bool(key) or before["video_folders"] != after["video_folders"]
                         or before.get("video_order") != after.get("video_order")
                         or before.get("fast_relay") != after.get("fast_relay"))  # 송출 경로 변경 → 영상만 재시작
        running = sup.proc(lv.id).desired
        if running and before["audio_folders"] != after["audio_folders"]:
            liquidsoap.write_m3u(lv, paths)  # 음악 폴더 변경은 목록만 바꾸면 Liquidsoap 이 다음 곡부터 반영 (끊김 없음)
        restarted = apply == "now" and running and (audio_changed or video_changed)
        if restarted:
            # 바뀐 것만 다시 띄운다: 재생 방식·크로스페이드 → 전체(약 8초), 비디오·키 → 영상만(2~3초).
            # 음악 폴더·이름·타이머는 재시작 없음
            sup.restart(lv.id, "설정 적용", audio_too=audio_changed)
        return {"ok": True, "restarted": bool(restarted)}

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
                sup.restart(live_id, "사용자 재시작", audio_too=False)
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

    def _fps_label(v) -> str:
        try:
            n, d = str(v).split("/")
            return f"{float(n) / float(d):g}fps"
        except (ValueError, ZeroDivisionError):
            return f"{v}fps"

    @app.get("/api/media/{kind}")
    def media(request: Request, kind: str):
        if not _kind(kind):
            return err("잘못된 종류", 404)
        usage = store.folder_usage(kind)
        folders = []
        for f in catalog.list_folders(paths, kind):
            files = catalog.list_files(paths, kind, f)
            pend = paths.incoming / kind / f
            fmts = set()
            if kind == "video":  # 해상도·fps 조합 (라이브 설정에서 섞임 경고용)
                for r in request.state.conn.execute("SELECT info FROM media_files WHERE kind='video' AND folder=?", (f,)):
                    try:
                        i = json.loads(r["info"] or "{}")
                        fmts.add(f"{i.get('width')}x{i.get('height')} {_fps_label(i.get('fps'))}")
                    except (ValueError, TypeError):
                        pass
            folders.append({"name": f, "count": len(files), "size": sum(p.stat().st_size for p in files),
                            "formats": sorted(fmts),
                            "used_by": usage.get(f, []),
                            "pending": len([p for p in pend.iterdir() if p.is_file()]) if pend.is_dir() else 0})
        rej = [{"id": r["id"], "ts": r["ts"], "folder": r["folder"], "name": r["name"], "reasons": json.loads(r["reasons"])}
               for r in request.state.conn.execute(
                   "SELECT * FROM rejections WHERE kind=? AND ts > ? ORDER BY ts DESC LIMIT 10",
                   (kind, time.time() - 3 * 86400)).fetchall()]
        return {"folders": folders, "rejected": rej, "spec": settings["spec"][kind]}

    # 검증 실패 목록 지우기 (기록만 — 격리된 파일은 7일 뒤 자동 삭제)
    @app.delete("/api/rejections/{kind}")
    def rejections_clear(request: Request, kind: str):
        if not _kind(kind):
            return err("잘못된 종류", 404)
        request.state.conn.execute("DELETE FROM rejections WHERE kind=?", (kind,))
        return {"ok": True}

    @app.delete("/api/rejections/{kind}/{rid}")
    def rejection_dismiss(request: Request, kind: str, rid: int):
        request.state.conn.execute("DELETE FROM rejections WHERE kind=? AND id=?", (kind, rid))
        return {"ok": True}

    @app.get("/api/media/{kind}/{folder}")
    def media_folder(request: Request, kind: str, folder: str):
        if not _kind(kind) or not config.valid_folder_name(folder):
            return err("잘못된 폴더", 404)
        conn = request.state.conn
        out = []
        # 곡 수가 많아도 가볍게: DB 는 폴더 단위로 한 번, '지금 곡·다음 곡' 파일은 라이브마다 한 번만 읽는다
        rows = {r["path"]: r for r in conn.execute(
            "SELECT path, duration, bitrate_kbps, thumb, added_at FROM media_files WHERE kind=? AND folder=?", (kind, folder))}
        playing: dict[str, list] = {}
        nexts: dict[str, list] = {}
        if kind == "audio":
            for lv in store.all():
                if folder not in lv.audio_folders or not sup.proc(lv.id).desired:
                    continue
                cur = (read_json(paths.run_dir / f"{lv.id}.nowplaying.json") or {}).get("file")
                nxt = (read_json(liquidsoap.next_path(lv, paths)) or {}).get("file")
                if cur:
                    playing.setdefault(cur, []).append(_who(lv))
                if nxt and nxt != cur:
                    nexts.setdefault(nxt, []).append(_who(lv))
        for p in catalog.list_files(paths, kind, folder):
            try:
                st = p.stat()
            except OSError:
                continue
            r = rows.get(str(p))
            out.append({"name": p.name, "size": st.st_size, "duration": r["duration"] if r else None,
                        "bitrate": r["bitrate_kbps"] if r else None, "thumb": bool(r and r["thumb"]),
                        "added_at": r["added_at"] if r else st.st_mtime,
                        "v": int(st.st_mtime),  # 썸네일 주소에 붙여 같은 이름을 다시 올려도 새 그림이 보이게
                        "playing": playing.get(str(p), []), "next": nexts.get(str(p), [])})
        pend_dir = paths.incoming / kind / folder
        pending = sorted(p.name for p in pend_dir.iterdir() if p.is_file()) if pend_dir.is_dir() else []
        return {"files": out, "pending": pending}

    # ------------------------------------------------------------------ 링크로 가져오기
    @app.post("/api/media/{kind}/{folder}/fetch")
    def fetch_add(kind: str, folder: str, b: dict = Body(default={})):
        if not _kind(kind) or not config.valid_folder_name(folder) or not (paths.kind_root(kind) / folder).is_dir():
            return err("폴더 없음")
        urls = [u.strip() for u in str(b.get("urls", "")).splitlines() if u.strip()][:50]
        if not urls:
            return err("링크를 넣으세요 (한 줄에 하나)")
        bad = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
        if bad:
            return err(f"http(s) 링크가 아닙니다: {bad[0][:60]}")
        return {"ok": True, "jobs": core.fetcher.add(kind, folder, urls)}

    @app.get("/api/fetch")
    def fetch_list():
        return {"jobs": core.fetcher.list()}

    @app.delete("/api/fetch/{jid}")
    def fetch_cancel(jid: int):
        return {"ok": core.fetcher.cancel(jid)}

    @app.post("/api/media/{kind}")
    def folder_create(request: Request, kind: str, b: dict = Body(default={})):
        name = str(b.get("name", "")).strip()
        if not _kind(kind) or not config.valid_folder_name(name):
            return err("폴더 이름은 문자/숫자로 시작하고 문자·숫자·공백·._-() 만 쓸 수 있습니다")
        (paths.kind_root(kind) / name).mkdir(parents=True, exist_ok=True)
        (paths.incoming / kind / name).mkdir(parents=True, exist_ok=True)
        return {"ok": True}

    @app.put("/api/media/{kind}/{folder}")
    def folder_rename(request: Request, kind: str, folder: str, b: dict = Body(default={})):
        new = str(b.get("name", "")).strip()
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
    def folder_delete(request: Request, kind: str, folder: str, confirm: str = ""):
        """폴더와 안의 파일을 모두 영구 삭제 (휴지통 없음). 실수 방지로 폴더 이름을 그대로 다시 받아야 한다."""
        if not _kind(kind) or not config.valid_folder_name(folder):
            return err("폴더 이름 오류")
        used = store.folder_usage(kind).get(folder)
        if used:
            names = ", ".join(lv.name for lv in store.all() if lv.id in used)
            return err(f"'{names}' 라이브에서 쓰는 폴더라 지울 수 없습니다 — 라이브 설정에서 이 폴더를 먼저 빼세요")
        if confirm != folder:
            return err("확인용 폴더 이름이 일치하지 않습니다")
        root = paths.kind_root(kind) / folder
        files = [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
        size = sum(p.stat().st_size for p in files)
        # 원본 · 업로드 대기 · 썸네일 · 격리 · 카탈로그 기록까지 남김없이
        for d in (root, paths.incoming / kind / folder, paths.thumbs_dir / kind / folder, paths.rejected / kind / folder):
            shutil.rmtree(d, ignore_errors=True)
        for meta in paths.uploads.glob("*.json"):  # 이 폴더로 올리다 끊긴 조각
            try:
                m = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if m.get("kind") == kind and m.get("folder") == folder:
                meta.with_suffix(".part").unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
        conn = request.state.conn
        conn.execute("DELETE FROM media_files WHERE kind=? AND folder=?", (kind, folder))
        conn.execute("DELETE FROM rejections WHERE kind=? AND folder=?", (kind, folder))
        if root.exists():
            return err("일부 파일을 지우지 못했습니다 — 다시 시도하세요", 500)
        msg = f"{'비디오' if kind == 'video' else '음악'} 폴더 '{folder}' 삭제 — 파일 {len(files)}개, {size / 1e6:.0f}MB"
        db.add_event(conn, "info", "web", msg)
        return {"ok": True, "message": msg}

    @app.delete("/api/media/{kind}/{folder}/{name}")
    def file_delete(request: Request, kind: str, folder: str, name: str):
        if not _kind(kind) or not config.valid_folder_name(folder) or "/" in name or name.startswith("."):
            return err("잘못된 요청")
        p = paths.kind_root(kind) / folder / name
        if not p.is_file():
            return err("파일 없음", 404)
        conn = request.state.conn
        used = store.folder_usage(kind).get(folder)
        if used and kind == "video":
            # 비디오는 송출 중인 24시간 목록에 이미 들어가 있어 중간에 지우면 그 차례에 송출이 끊긴다
            names = ", ".join(lv.name for lv in store.all() if lv.id in used)
            return err(f"'{names}' 라이브에서 쓰는 비디오 폴더라 지울 수 없습니다 — 라이브 설정에서 이 폴더를 빼고 지우세요")
        if used and kind == "audio":
            lives = [lv for lv in store.all() if lv.id in used]
            busy = _song_busy(lives, p)
            if busy:
                return err(busy)
            # 경쟁 상태 방지: ① 이 곡을 목록에서 먼저 빼고 ② Liquidsoap 이 목록을 다시 읽을 시간을 준 뒤
            # ③ 그 사이 '다음 곡'으로 뽑히지 않았는지 다시 확인하고 ④ 지운다. 뽑혔으면 취소하고 목록 복구.
            liquidsoap.DELETING.add(str(p))
            try:
                for lv in lives:
                    liquidsoap.write_m3u(lv, paths)
                time.sleep(1.5)
                busy = _song_busy(lives, p)
                if busy:
                    return err(busy)
                p.unlink()  # 혹시 재생 중이었더라도 리눅스는 열린 파일을 끝까지 읽게 해 준다 (소리가 끊기지 않음)
            finally:
                liquidsoap.DELETING.discard(str(p))
                for lv in lives:
                    liquidsoap.write_m3u(lv, paths)
        else:
            p.unlink()
        catalog.remove(conn, p)
        if kind == "video":
            thumbs.thumb_path(paths, folder, name).unlink(missing_ok=True)
        return {"ok": True}

    def _who(lv: Live) -> dict:
        ch = chstore.get(lv.channel_id) if lv.channel_id else None
        return {"live": lv.name, "channel": ch.name if ch else ""}

    def _song_state(lv: Live, p: Path) -> str | None:
        """이 곡이 그 라이브에서 지금 재생 중('playing')인지, 다음 곡으로 골라졌는지('next')."""
        if not sup.proc(lv.id).desired:
            return None
        cur = (read_json(paths.run_dir / f"{lv.id}.nowplaying.json") or {}).get("file")
        nxt = (read_json(liquidsoap.next_path(lv, paths)) or {}).get("file")
        if cur == str(p):
            return "playing"
        if nxt == str(p):
            return "next"
        return None

    def _song_busy(lives: list, p: Path) -> str | None:
        for lv in lives:
            st = _song_state(lv, p)
            if st:
                what = "지금 재생 중인" if st == "playing" else "바로 다음에 나올"
                return f"'{lv.name}' 라이브에서 {what} 곡이라 지금은 지울 수 없습니다 — 곡이 바뀐 뒤 다시 지우세요"
        return None

    @app.get("/api/thumbs/{folder}/{name}")
    def thumb(request: Request, folder: str, name: str):
        if not config.valid_folder_name(folder) or "/" in name or name.startswith("."):
            return err("bad", 400)
        p = paths.thumbs_dir / "video" / folder / f"{name}.jpg"
        if not p.is_file():
            return err("not found", 404)
        # 주소에 판(v)이 붙어 있으면 그림이 바뀌면 주소도 바뀌므로 오래 기억해도 되고,
        # 판이 없으면(라이브 미리보기) 매번 바뀌었는지만 확인한다 — 같은 이름을 다시 올려도 옛 그림이 안 남게
        cc = "private, max-age=604800, immutable" if request.query_params.get("v") else "private, no-cache"
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": cc})

    # ------------------------------------------------------------------ 이어 올리기 업로드
    def _upload_id(kind: str, folder: str, name: str, size: int) -> str:
        return hashlib.sha1(f"{kind}/{folder}/{name}/{size}".encode()).hexdigest()[:24]

    @app.post("/api/uploads")
    def upload_begin(request: Request, b: dict = Body(default={})):
        kind, folder, size = str(b.get("kind")), str(b.get("folder")), int(b.get("size", 0))
        name = filenames.normalize(Path(str(b.get("name", "")).replace("\\", "/")).name)
        if not _kind(kind) or not config.valid_folder_name(folder) or not (paths.kind_root(kind) / folder).is_dir():
            return err("폴더 없음")
        bad = filenames.problems(name, kind)
        if bad:
            return err(f"'{name}' — " + " · ".join(bad))
        if size <= 0 or size > int(settings["dashboard"]["max_upload_mb"]) * 1024 * 1024:
            return err("파일 크기 오류")
        # 같은 이름은 올리기 전에 바로 알려 준다 (덮어쓰지 않음 — 송출 중일 수 있으므로)
        for d in (paths.kind_root(kind) / folder, paths.incoming / kind / folder):
            if d.is_dir() and any(f.name.lower() == name.lower() for f in d.iterdir() if f.is_file()):
                where = "방송 폴더" if d.parent.parent != paths.incoming else "검증 대기"
                return err(f"'{name}' 과(와) 같은 이름의 파일이 이미 있습니다 ({where}) — 교체하려면 기존 파일을 지운 뒤 올리거나 이름을 바꾸세요")
        uid = _upload_id(kind, folder, name, size)
        meta = paths.uploads / f"{uid}.json"  # 저장되는 이름은 NFC 로 합친 이름
        part = paths.uploads / f"{uid}.part"
        if not meta.exists():
            meta.write_text(json.dumps({"kind": kind, "folder": folder, "name": name, "size": size}))
            part.touch()
        return {"id": uid, "offset": part.stat().st_size if part.exists() else 0}

    # 웹 업로드 속도 상한 — 여러 파일을 동시에 올려도 합쳐서 이 속도를 넘지 않게 (받는 쪽 CPU·디스크 부담을 고르게)
    _bucket = {"t": 0.0}

    async def _throttle(n: int) -> None:
        rate = float(settings["dashboard"].get("upload_mb_per_s") or 0) * 1024 * 1024
        if rate <= 0:
            return
        now = time.monotonic()
        _bucket["t"] = max(_bucket["t"], now) + n / rate
        delay = _bucket["t"] - now
        if delay > 0.02:
            await asyncio.sleep(delay)

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
        f = await run_in_threadpool(open, part, "ab")
        try:
            buf = bytearray()
            async for chunk in request.stream():
                await _throttle(len(chunk))
                buf += chunk
                if len(buf) >= 1 << 20:  # 1MB 씩 모아 스레드에서 쓴다 (디스크가 느려도 다른 요청은 안 막힘)
                    await run_in_threadpool(f.write, bytes(buf))
                    buf.clear()
                    if f.tell() > size:
                        break
            if buf:
                await run_in_threadpool(f.write, bytes(buf))
        finally:
            await run_in_threadpool(f.close)
        new = part.stat().st_size
        if new > size:
            part.unlink(); meta.unlink()
            return err("크기 초과 — 다시 올려 주세요")
        return {"ok": True, "offset": new}

    @app.delete("/api/uploads/{uid}")
    def upload_cancel(uid: str):
        """올리다 만 조각을 지운다 (웹의 '취소'). 이미 완료되어 검증 대기로 넘어간 파일은 건드리지 않는다."""
        if not uid.isalnum():
            return err("bad id")
        (paths.uploads / f"{uid}.part").unlink(missing_ok=True)
        (paths.uploads / f"{uid}.json").unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/uploads/{uid}/complete")
    def upload_complete(uid: str):
        if not uid.isalnum():
            return err("bad id")
        meta, part = paths.uploads / f"{uid}.json", paths.uploads / f"{uid}.part"
        if not meta.exists() or not part.exists():
            return err("업로드 정보 없음", 404)
        m = json.loads(meta.read_text())
        if not (paths.kind_root(m["kind"]) / m["folder"]).is_dir():
            part.unlink(missing_ok=True); meta.unlink(missing_ok=True)
            return err("폴더가 삭제되었습니다", 404)
        if part.stat().st_size != m["size"]:
            return err("아직 다 올라가지 않았습니다")
        dest_dir = paths.incoming / m["kind"] / m["folder"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / m["name"]
        try:
            os.link(part, dest)  # 같은 이름이 이미 있으면 실패 (덮어쓰지 않음)
        except FileExistsError:
            part.unlink(missing_ok=True); meta.unlink(missing_ok=True)
            return err(f"'{m['name']}' 과(와) 같은 이름의 파일이 이미 검증 대기 중입니다")
        part.unlink()  # 완성된 파일만 스테이징에 나타난다 → ingest 가 검증 후 방송 폴더로
        meta.unlink()
        return {"ok": True, "message": "업로드 완료 — 규격 검증 중"}

    # ------------------------------------------------------------------ 시스템 / 설정
    @app.get("/api/system")
    def system(request: Request):
        conn = request.state.conn
        disk = []
        for label, p in (("비디오", paths.video), ("음악", paths.audio), ("업로드 대기", paths.incoming),
                         ("격리", paths.rejected), ("로그", paths.logs), ("상태·DB", paths.state)):
            disk.append({"label": label, "bytes": sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0})
        sess = request.state.session
        return {
            "version": core.version, "uptime_since": core.started_at, "disk": disk,
            "reboot_required": REBOOT_FLAG.exists(),
            "reboot_packages": REBOOT_FLAG.with_suffix(".pkgs").read_text().split() if REBOOT_FLAG.with_suffix(".pkgs").exists() else [],
            "settings": {"restart_base_utc": settings["restart"]["base_utc"], "server_name": config.server_name(settings),
                         "max_lives": _max_lives(), "tls_relay": settings["output"]["tls_relay"]["enabled"],
                         "port_mbit": settings["capacity"].get("port_mbit", 1000),
                         "monthly_traffic_tb": settings["capacity"].get("monthly_traffic_tb", 0),
                         # 안전선 (용량 계산·서버 박스 막대 눈금)
                         "mem_reserve_mb": settings["capacity"].get("mem_reserve_mb", 700),
                         "cpu_target_pct": settings["capacity"].get("cpu_target_pct", 50),
                         "port_use_pct": round(float(settings["capacity"].get("port_use_ratio", 0.8)) * 100),
                         "disk_warn_pct": settings["capacity"].get("disk_warn_pct", 85)},
            "sessions": [{"id": r["token_hash"], "ip": r["ip"], "ua": r["user_agent"], "last_seen": r["last_seen"],
                          "current": r["token_hash"] == sess["token_hash"]}
                         for r in conn.execute("SELECT * FROM sessions ORDER BY last_seen DESC").fetchall()],
            "attempts": [dict(r) for r in conn.execute("SELECT ts, ip, username, success FROM login_attempts ORDER BY ts DESC LIMIT 30").fetchall()],
            "update_requested": (paths.state / "update.request").exists(),
            "update_last": _update_last(),
            "update_check": _update_check(),
            "notify": _notify_view(),
        }

    def _update_check() -> dict:
        uc = getattr(core, "updates", None)
        if not uc:
            return {"status": "unknown"}
        uc.refresh_async()
        st = dict(uc.state)
        st["latest_short"] = (st.get("latest") or "")[:7]
        return st

    def _update_last() -> dict | None:
        """호스트가 남긴 logs/update.log 의 마지막 업데이트 결과 (웹 버튼으로 요청한 것)."""
        f = paths.logs / "update.log"
        if not f.exists():
            return None
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 20000))
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            return None
        marker = "웹 요청으로 업데이트"
        i = text.rfind(marker)
        if i < 0:
            return None
        block = text[i:]
        ts_text = text[max(0, text.rfind("\n", 0, i) + 1):i].strip()
        if "업데이트 실패" in block:
            result = "failed"
        elif "Image is up to date" in block:
            result = "latest"
        elif "Downloaded newer image" in block:
            result = "updated"
        else:
            result = "running"
        return {"at": ts_text, "result": result}

    def _notify_view() -> dict:
        cfg = notifier.cfg
        tok, tgt = notify.read_token(paths), notify.read_tg_token(paths)
        return {"enabled": bool(cfg.get("enabled")), "label": notifier.label(),
                "categories": dict(cfg["categories"]), "token_masked": secrets.mask(tok) if tok else "",
                "last_error": notifier.last_error,
                "tg_enabled": bool(cfg.get("telegram_enabled")), "tg_token_masked": secrets.mask(tgt) if tgt else "",
                "tg_chat": cfg.get("telegram_chat_name", "") if cfg.get("telegram_chat_id") else "",
                "tg_commands": bool(cfg.get("telegram_commands", True)), "tg_last_error": notifier.tg_last_error,
                "catalog": [{"key": k, "label": lb, "desc": ds} for k, lb, ds, _ in notify.CATEGORIES]}

    @app.put("/api/system/notify")
    def notify_settings(request: Request, b: dict = Body(default={})):
        tok = str(b.get("token") or "").strip()
        if tok:
            if len(tok) > 200 or not tok.isascii() or " " in tok:
                return err("Access Token 형식이 올바르지 않습니다")
            try:
                who = notify.check_token(tok)
            except notify.PushError as e:
                return err(str(e))
            secrets.write_secret(notify.token_file(paths), tok)
        elif b.get("clear_token"):
            notify.token_file(paths).unlink(missing_ok=True)
            who = None
        else:
            who = None
        cfg = notifier.cfg
        # 텔레그램: 토큰(바꿀 때만) · 켜기 · 명령 받기
        tgt = str(b.get("tg_token") or "").strip()
        tg_who = None
        if tgt:
            if len(tgt) > 100 or not tgt.isascii() or " " in tgt or ":" not in tgt:
                return err("텔레그램 봇 토큰 형식이 올바르지 않습니다 (예: 123456789:ABC...)")
            try:
                tg_who = notify.tg_check(tgt)
            except notify.PushError as e:
                return err(str(e))
            if tgt != notify.read_tg_token(paths):  # 봇이 바뀌면 대화방 연결도 새로
                cfg["telegram_chat_id"], cfg["telegram_chat_name"] = "", ""
                config.save_settings_override(paths, ["notify", "telegram_chat_id"], "")
                config.save_settings_override(paths, ["notify", "telegram_chat_name"], "")
            secrets.write_secret(notify.tg_token_file(paths), tgt)
        elif b.get("clear_tg_token"):
            notify.tg_token_file(paths).unlink(missing_ok=True)
            for k, v in (("telegram_enabled", False), ("telegram_chat_id", ""), ("telegram_chat_name", "")):
                cfg[k] = v
                config.save_settings_override(paths, ["notify", k], v)
        if "tg_enabled" in b:
            if b["tg_enabled"] and not (notify.tg_token_file(paths).exists() and cfg.get("telegram_chat_id")):
                return err("텔레그램은 봇 토큰을 넣고 '연결 확인'을 먼저 하세요")
            cfg["telegram_enabled"] = bool(b["tg_enabled"])
            config.save_settings_override(paths, ["notify", "telegram_enabled"], cfg["telegram_enabled"])
        if "tg_commands" in b:
            cfg["telegram_commands"] = bool(b["tg_commands"])
            config.save_settings_override(paths, ["notify", "telegram_commands"], cfg["telegram_commands"])
        if "enabled" in b:
            if b["enabled"] and not notify.token_file(paths).exists():
                return err("Access Token 을 먼저 입력하세요")
            cfg["enabled"] = bool(b["enabled"])
            config.save_settings_override(paths, ["notify", "enabled"], cfg["enabled"])
        if isinstance(b.get("categories"), dict):
            known = {k for k, *_ in notify.CATEGORIES}
            for k, v in b["categories"].items():
                if k in known:
                    cfg["categories"][k] = bool(v)
            config.save_settings_override(paths, ["notify", "categories"], dict(cfg["categories"]))
        db.add_event(request.state.conn, "info", "web", "알림 설정 변경" + (" (토큰 교체)" if tok else ""))
        extra = "".join(x for x in (f" (Pushbullet 계정: {who})" if who else "", f" (텔레그램 봇: {tg_who})" if tg_who else ""))
        return {"ok": True, "message": f"저장했습니다{extra}", "notify": _notify_view()}

    @app.post("/api/system/notify/telegram/connect")
    def telegram_connect(request: Request, b: dict = Body(default={})):
        """'연결 확인': (새 토큰이 있으면 확인·저장하고) 봇에게 말을 건 대화방을 찾아 저장 + 테스트 메시지."""
        tgt = str(b.get("tg_token") or "").strip() or notify.read_tg_token(paths)
        if not tgt:
            return err("텔레그램 봇 토큰을 먼저 입력하세요")
        try:
            bot = notify.tg_check(tgt)
            chat_id, name = notify.tg_find_chat(tgt)
            notifier.tg_send(f"[{notifier.label()}] LiveMax 와 연결됐습니다 ✅\n이제 이 대화방으로 알림이 옵니다.\n명령: /상태  /오늘  /라이브 번호", chat_id=chat_id, token=tgt)
        except notify.PushError as e:
            return err(str(e))
        secrets.write_secret(notify.tg_token_file(paths), tgt)
        cfg = notifier.cfg
        for k, v in (("telegram_chat_id", chat_id), ("telegram_chat_name", name), ("telegram_enabled", True)):
            cfg[k] = v
            config.save_settings_override(paths, ["notify", k], v)
        db.add_event(request.state.conn, "info", "web", f"텔레그램 알림 연결 ({bot})")
        return {"ok": True, "message": f"{bot} 봇과 '{name}' 대화방을 연결하고 알림을 켰습니다. 휴대폰을 확인하세요", "notify": _notify_view()}

    @app.post("/api/system/notify/telegram/test")
    def telegram_test():
        try:
            return {"ok": True, "message": notifier.tg_test()}
        except notify.PushError as e:
            return err(str(e))

    @app.post("/api/system/notify/test")
    def notify_test():
        try:
            return {"ok": True, "message": notifier.test()}
        except notify.PushError as e:
            return err(str(e))

    @app.put("/api/system/settings")
    def system_settings(request: Request, b: dict = Body(default={})):
        if "restart_base_utc" in b:
            try:
                config.hhmm_to_minutes(b["restart_base_utc"])
            except ConfigError as e:
                return err(str(e))
            config.save_settings_override(paths, ["restart", "base_utc"], b["restart_base_utc"])
            settings["restart"]["base_utc"] = b["restart_base_utc"]
        if "server_name" in b:
            name = str(b["server_name"] or "").strip()[:30]
            if any(c in name for c in "<>\"'`"):
                return err("서버명에 < > \" ' ` 는 쓸 수 없습니다")
            config.save_settings_override(paths, ["server_name"], name)
            settings["server_name"] = name
        if "max_lives" in b:
            try:
                n = int(b["max_lives"])
            except (TypeError, ValueError):
                return err("라이브 최대 개수는 숫자로 입력하세요")
            if not 1 <= n <= 100:
                return err("라이브 최대 개수는 1~100 사이로 입력하세요")
            config.save_settings_override(paths, ["max_lives"], n)
            settings["max_lives"] = n
        # 용량 계산 기준 (업체마다 다름): 회선 속도(Mbps), 월 트래픽 한도(TB, 0 = 무제한)
        # + 안전선: 메모리 남길 여유(MB) · CPU 평균(%) · 회선 사용(%) · 디스크 경고(%)
        for key, lo, hi, label in (("port_mbit", 10, 100000, "회선 속도(Mbps)"), ("monthly_traffic_tb", 0, 10000, "월 트래픽 한도(TB)"),
                                   ("mem_reserve_mb", 100, 100000, "메모리 남길 여유(MB)"), ("cpu_target_pct", 10, 95, "CPU 안전선(%)"),
                                   ("port_use_pct", 10, 100, "회선 안전선(%)"), ("disk_warn_pct", 50, 99, "디스크 안전선(%)")):
            if key in b:
                try:
                    v = float(b[key])
                except (TypeError, ValueError):
                    return err(f"{label}는 숫자로 입력하세요")
                if not lo <= v <= hi:
                    return err(f"{label}는 {lo}~{hi} 사이로 입력하세요 (트래픽 0 = 무제한)")
                v = int(v) if v == int(v) else v
                if key == "port_use_pct":  # 화면은 %, 저장은 비율
                    key, v = "port_use_ratio", round(v / 100, 3)
                config.save_settings_override(paths, ["capacity", key], v)
                settings["capacity"][key] = v
        if b.get("lives_view") in ("grid", "list", "cctv"):
            config.save_settings_override(paths, ["ui", "lives_view"], b["lives_view"])
            settings.setdefault("ui", {})["lives_view"] = b["lives_view"]
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
        return await run_in_threadpool(_import_zip, await request.body())

    def _import_zip(raw: bytes):
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            return err("zip 파일이 아닙니다")
        added = []
        for n in z.namelist():
            if not (n.startswith("lives/") and n.endswith(".yaml")):
                continue
            if len(store.all()) >= _max_lives():
                return {"ok": True, "added": added, "message": f"{len(added)}개를 가져오다 라이브 최대 개수({_max_lives()}개)에 도달해 멈췄습니다"}
            src = live_from_dict(yaml.safe_load(z.read(n)))
            lv = store.new(src.name)  # 새 서버 기준으로 ID·포트·슬롯 재배정
            for k in ("video_folders", "audio_folders", "audio_mode", "norepeat_count", "crossfade",
                      "timer_mode", "timer_minutes", "timer_repeat", "timer_gap_minutes"):
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
