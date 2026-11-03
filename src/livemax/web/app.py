"""대시보드 앱 — livemax-dashboard.service

    uvicorn 이 127.0.0.1:8080 에만 바인드하고, 외부 HTTPS 는 Caddy(Let's Encrypt)가 담당한다.

1단계 범위 (DESIGN 8.1): 조회 + 미디어 관리 + 라이브 설정. 시작·정지 같은 조작 버튼은 2단계.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import config, db, logsetup, secrets
from . import auth, data, geo
from .common import BASE, client_ip, redirect, render

log = logging.getLogger("livemax.web")

PUBLIC_PATHS = ("/login", "/static/", "/healthz")


def create_app() -> FastAPI:
    server = config.load_server()
    paths = config.get_paths(server)
    db.init(paths.db)

    app = FastAPI(title="LiveMax", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.server = server
    app.state.paths = paths
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next):
        conn = db.connect(paths.db)
        request.state.conn = conn
        request.state.server = server
        request.state.paths = paths
        try:
            ip, tunnel = client_ip(request)
            request.state.ip = ip
            request.state.tunnel = tunnel

            # 1) 지역 차단
            allowed, why = geo.check(conn, server, ip, tunnel)
            if not allowed:
                log.warning("지역 차단 %s (%s) %s", ip, why, request.url.path)
                return PlainTextResponse("Forbidden", status_code=403)
            if why == "geo-db-missing":
                request.state.geo_warning = True

            # 2) CSRF — 상태 변경 요청은 같은 출처에서만 (쿠키도 SameSite=Strict)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin") or request.headers.get("referer")
                host = request.headers.get("host", "")
                if not origin or urlparse(origin).netloc != host:
                    log.warning("CSRF 차단 %s origin=%s host=%s", ip, origin, host)
                    return PlainTextResponse("Bad origin", status_code=403)

            # 3) 인증
            path = request.url.path
            if not path.startswith(PUBLIC_PATHS):
                sess = auth.get_session(conn, request.cookies.get(auth.COOKIE))
                if not sess:
                    if path.startswith("/api/"):
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                    return RedirectResponse("/login", status_code=303)
                request.state.session = sess
                # 첫 로그인: 비밀번호 변경 강제 (DESIGN 9.2)
                if auth.must_change() and path not in ("/settings/password", "/logout"):
                    return redirect("/settings/password", err="초기 비밀번호를 먼저 변경하세요")
            response = await call_next(request)
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            return response
        finally:
            conn.close()

    # ---------------- 로그인 ----------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login")
    def login_page(request: Request):
        return render(request, "login.html", bare=True)

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        conn = request.state.conn
        ip = request.state.ip
        dcfg = server["dashboard"]
        remain = auth.blocked_seconds(conn, ip, int(dcfg["login_max_failures"]), int(dcfg["login_block_minutes"]))
        if remain:
            log.warning("로그인 차단 중 IP %s (%ds 남음)", ip, remain)
            return render(request, "login.html", bare=True, err=f"로그인 실패가 많아 {remain // 60 + 1}분간 차단되었습니다")
        ok = username == dcfg["username"] and auth.verify_password(password)
        auth.record_attempt(conn, ip, username, ok)
        if not ok:
            log.warning("로그인 실패 %s user=%s", ip, username[:32])
            return render(request, "login.html", bare=True, err="아이디 또는 비밀번호가 올바르지 않습니다")
        token = auth.create_session(conn, ip, request.headers.get("user-agent", ""), float(dcfg["session_hours"]))
        log.info("로그인 성공 %s", ip)
        resp = RedirectResponse("/settings/password" if auth.must_change() else "/", status_code=303)
        resp.set_cookie(auth.COOKIE, token, httponly=True, secure=True, samesite="strict",
                        max_age=int(float(dcfg["session_hours"]) * 3600), path="/")
        return resp

    @app.post("/logout")
    def logout(request: Request):
        sess = getattr(request.state, "session", None)
        if sess:
            auth.revoke(request.state.conn, sess["token_hash"])
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    # ---------------- 모니터링 / 유지관리 (기본 화면) ----------------
    def overview(request: Request) -> dict:
        conn = request.state.conn
        rows, errors = data.live_rows(conn, server, paths)
        sysd = data.system_snapshot(paths)
        cap = data.capacity_view(server, rows, sysd)
        counts = {k: sum(1 for r in rows if r["state"] == k) for k in data.STATES}
        return {"rows": rows, "errors": errors, "sys": sysd, "cap": cap, "counts": counts,
                "alerts": data.alerts(conn, server, rows, sysd, errors), "reboot": data.reboot_status()}

    @app.get("/")
    def monitor(request: Request):
        ov = overview(request)
        return render(request, "monitor.html", **ov, disk=data.disk_breakdown(paths),
                      log_names=_log_names(), geo_warning=getattr(request.state, "geo_warning", False))

    @app.get("/api/status")
    def api_status(request: Request):
        ov = overview(request)
        cap = ov["cap"]
        return {
            "ts": time.time(),
            "counts": ov["counts"],
            "system": ov["sys"],
            "capacity": {"can_add": cap.can_add, "limiting": cap.limiting, "projected_tb": cap.projected_tb,
                         "limit_tb": cap.limit_tb, "usage_ratio": cap.usage_ratio, "month_tx_tb": cap.month_tx_tb},
            "lives": [{k: v for k, v in r.items() if k != "cfg"} for r in ov["rows"]],
            "alerts": ov["alerts"][:30],
            "reboot": ov["reboot"],
        }

    @app.post("/maintenance/cleanup")
    def manual_cleanup(request: Request):
        # 자동 주기를 기다리지 않고 즉시 실행 (DESIGN 6.4). 낮은 I/O 우선순위로 별도 프로세스.
        cmd = ["ionice", "-c3", "nice", "-n", "19", sys.executable, "-m", "livemax.cleanup", "--manual"]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except FileNotFoundError:
            subprocess.Popen(cmd[5:], start_new_session=True)
        db.add_event(request.state.conn, "info", "dashboard", "수동 정리 실행 요청")
        return redirect("/", ok="정리를 시작했습니다. 결과는 경고/이벤트 목록과 cleanup 로그에 남습니다")

    def _log_names() -> list[str]:
        if not paths.log_dir.is_dir():
            return []
        return sorted(p.name for p in paths.log_dir.glob("*.log"))

    @app.get("/logs")
    def logs(request: Request, name: str = "", lines: int = 300):
        names = _log_names()
        if name not in names:
            return render(request, "logs.html", names=names, name="", content="")
        lines = max(10, min(lines, 3000))
        content = _tail(paths.log_dir / name, lines)
        return render(request, "logs.html", names=names, name=name, content=secrets.redact(content), lines=lines)

    @app.get("/thumbs/{folder}/{name}")
    def thumb(folder: str, name: str):
        if not config.valid_folder_name(folder) or "/" in name or name.startswith("."):
            return PlainTextResponse("bad", status_code=400)
        p = paths.thumbs_dir / "video" / folder / name
        if not p.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    from . import live_routes, media_routes, settings_routes

    app.include_router(media_routes.router)
    app.include_router(live_routes.router)
    app.include_router(settings_routes.router)
    return app


def _tail(p: Path, n: int) -> str:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, n * 300)
            f.seek(size - block)
            return b"\n".join(f.read().splitlines()[-n:]).decode("utf-8", "replace")
    except OSError as e:
        return f"(읽기 실패: {e})"


def main() -> int:
    import uvicorn

    server = config.load_server()
    logsetup.setup("dashboard", server)
    d = server["dashboard"]
    uvicorn.run(create_app(), host=d["bind"], port=int(d["port"]), proxy_headers=False, access_log=False,
                log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
