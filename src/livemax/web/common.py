"""대시보드 공통: 템플릿, 클라이언트 IP 판별, 응답 헬퍼."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from ..secrets import mask

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["mask"] = mask


def _fnum(v, digits=1, unit=""):
    if v is None or isinstance(v, Undefined):
        return "-"
    try:
        return f"{float(v):.{digits}f}{unit}"
    except (TypeError, ValueError):
        return "-"


templates.env.filters["num"] = _fnum
templates.env.filters["ts"] = lambda t: time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"
LOOPBACK = {"127.0.0.1", "::1"}


def client_ip(request: Request) -> tuple[str, bool]:
    """(클라이언트 IP, SSH 터널 여부).

    대시보드는 127.0.0.1 에만 바인드한다. 외부 요청은 Caddy 가 X-Forwarded-For 를 붙여 넘긴다.
    루프백에서 XFF 없이 들어온 요청 = SSH 터널(또는 서버 로컬) → 지역 차단 우회 (DESIGN 9.3).
    """
    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    if peer in LOOPBACK:
        if xff:
            return xff.split(",")[-1].strip(), False
        return peer, True
    return peer, False


def redirect(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if ok:
        url += f"{sep}ok={quote(ok)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def render(request: Request, _template: str, /, **ctx):
    ctx.setdefault("ok", request.query_params.get("ok"))
    ctx.setdefault("err", request.query_params.get("err"))
    ctx.setdefault("path", request.url.path)
    return templates.TemplateResponse(request, _template, ctx)
