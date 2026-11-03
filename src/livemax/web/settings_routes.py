"""보안·시스템 설정 — /settings (DESIGN 8.3 ①, 9, 10.3)

- 비밀번호 변경 (첫 로그인 시 강제)
- 지역 차단 ON/OFF, 임시 허용 IP 등록·삭제
- 세션 목록 및 강제 로그아웃, 최근 로그인 시도
- 설정 내보내기/가져오기 — 스트림 키는 절대 포함하지 않는다
"""

from __future__ import annotations

import io
import ipaddress
import time
import zipfile

import yaml
from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import Response

from .. import config, db, paths as _paths
from . import auth, geo
from .common import redirect, render

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request):
    conn, server = request.state.conn, request.state.server
    sess = request.state.session
    return render(
        request, "settings.html",
        geo_on=geo.geo_enabled(conn, server),
        geo_db=bool(geo._reader(server["geo"]["mmdb_path"])),
        allowed_countries=server["geo"]["allowed_countries"],
        allow_ips=conn.execute("SELECT * FROM allow_ips ORDER BY created DESC").fetchall(),
        sessions=conn.execute("SELECT * FROM sessions ORDER BY last_seen DESC").fetchall(),
        attempts=conn.execute("SELECT * FROM login_attempts ORDER BY ts DESC LIMIT 50").fetchall(),
        current=sess["token_hash"], my_ip=request.state.ip, tunnel=request.state.tunnel,
        fmt=lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-",
    )


@router.get("/settings/password")
def password_page(request: Request):
    return render(request, "password.html", forced=auth.must_change())


@router.post("/settings/password")
def password_change(request: Request, current: str = Form(...), new: str = Form(...), new2: str = Form(...)):
    server = request.state.server
    if not auth.verify_password(current):
        return render(request, "password.html", forced=auth.must_change(), err="현재 비밀번호가 틀립니다")
    if new != new2:
        return render(request, "password.html", forced=auth.must_change(), err="새 비밀번호가 서로 다릅니다")
    if new == current:
        return render(request, "password.html", forced=auth.must_change(), err="이전과 다른 비밀번호를 쓰세요")
    probs = auth.password_problems(new, server["dashboard"]["username"])
    if probs:
        return render(request, "password.html", forced=auth.must_change(), err=" / ".join(probs))
    auth.set_password(new)
    # 다른 세션은 모두 로그아웃
    auth.revoke_all_except(request.state.conn, request.state.session["token_hash"])
    db.add_event(request.state.conn, "info", "dashboard", "관리자 비밀번호 변경")
    return redirect("/settings", ok="비밀번호를 변경했습니다. 다른 세션은 모두 로그아웃되었습니다")


@router.post("/settings/geo")
def geo_toggle(request: Request, enabled: str = Form("")):
    on = enabled == "on"
    db.set_setting(request.state.conn, "geo.enabled", "1" if on else "0")
    db.add_event(request.state.conn, "warn" if not on else "info", "dashboard", f"지역 차단 {'ON' if on else 'OFF'}")
    return redirect("/settings", ok=f"지역 차단 {'켜짐' if on else '꺼짐'}")


@router.post("/settings/allow-ip")
def allow_ip_add(request: Request, ip: str = Form(...), note: str = Form(""), days: int = Form(14)):
    ip = ip.strip()
    try:
        ipaddress.ip_network(ip, strict=False)
    except ValueError:
        return redirect("/settings", err="IP 형식 오류 (예: 203.0.113.7 또는 203.0.113.0/24)")
    expires = time.time() + days * 86400 if days > 0 else None
    request.state.conn.execute(
        "INSERT INTO allow_ips(ip, note, created, expires) VALUES (?,?,?,?) "
        "ON CONFLICT(ip) DO UPDATE SET note=excluded.note, expires=excluded.expires", (ip, note[:100], time.time(), expires))
    db.add_event(request.state.conn, "info", "dashboard", f"임시 허용 IP 등록 {ip} ({days}일)")
    return redirect("/settings", ok=f"{ip} 허용 ({days}일)")


@router.post("/settings/allow-ip/delete")
def allow_ip_del(request: Request, ip: str = Form(...)):
    request.state.conn.execute("DELETE FROM allow_ips WHERE ip=?", (ip,))
    return redirect("/settings", ok=f"{ip} 삭제")


@router.post("/settings/sessions/revoke")
def session_revoke(request: Request, token_hash: str = Form(...)):
    auth.revoke(request.state.conn, token_hash)
    return redirect("/settings", ok="세션을 종료했습니다")


# ---------------- 설정 내보내기 / 가져오기 (DESIGN 10.3) ----------------

@router.get("/settings/export")
def export_config(request: Request):
    """라이브 설정 묶음(zip). 스트림 키는 제외한다 — 설정 파일엔 키 '경로'만 있고 키 값은 secrets/ 에만 있다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in config.live_files():
            z.writestr(f"lives/{p.name}", p.read_text(encoding="utf-8"))
        sp = _paths.CONFIG_DIR / config.SERVER_FILE
        if sp.exists():
            z.writestr("server.yaml", sp.read_text(encoding="utf-8"))
        z.writestr("README.txt", "LiveMax 설정 내보내기. 스트림 키는 포함되어 있지 않습니다.\n"
                                 "새 서버의 대시보드 > 설정 > 가져오기 로 올린 뒤, 라이브마다 스트림 키를 입력하세요.\n")
    stamp = time.strftime("%Y%m%d-%H%M")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="livemax-config-{stamp}.zip"'})


@router.post("/settings/import")
async def import_config(request: Request, file: UploadFile, overwrite: str = Form("")):
    server = request.state.server
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        return redirect("/settings", err="파일이 너무 큽니다")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return redirect("/settings", err="zip 파일이 아닙니다")

    incoming: list[tuple[str, dict]] = []
    for n in z.namelist():
        if n.startswith("lives/") and n.endswith(".yaml") and "/" not in n[len("lives/"):]:
            d = yaml.safe_load(z.read(n).decode("utf-8"))
            incoming.append((n[len("lives/"):], d))
    if not incoming:
        return redirect("/settings", err="lives/*.yaml 이 없습니다")

    existing, _ = config.load_all_lives(server=server, cross=False)
    existing_ids = {lv.id for lv in existing}
    parsed = []
    for fname, d in incoming:
        try:
            d = dict(d)
            d["enabled"] = False  # 가져온 라이브는 키 입력 전까지 비활성 (키가 없으므로)
            lv = config.parse_live(d, _paths.CONFIG_DIR / fname, server)
        except config.ConfigError as e:
            return redirect("/settings", err=f"{fname}: {e}")
        if lv.id in existing_ids and overwrite != "on":
            continue
        parsed.append((lv, d))
    keep = [lv for lv in existing if lv.id not in {p[0].id for p in parsed}]
    problems = config.cross_check([*keep, *(p[0] for p in parsed)], server)
    if problems:
        return redirect("/settings", err=" / ".join(problems))
    from .live_routes import _write_yaml_atomic

    for lv, d in parsed:
        _write_yaml_atomic(_paths.CONFIG_DIR / f"{lv.id}.yaml", d)
    db.add_event(request.state.conn, "info", "dashboard", f"설정 가져오기: {', '.join(p[0].id for p in parsed) or '없음'}")
    return redirect("/live", ok=f"{len(parsed)}개 라이브 설정을 가져왔습니다 (비활성 상태). 각 라이브에서 스트림 키를 입력하고 활성화하세요")
