"""지역 차단 (DESIGN 9.3).

- 기본 ON (국내만). 설정 화면에서 끌 수 있다 (DB settings 'geo.enabled' 가 server.yaml 값을 덮어씀).
- 임시 허용 IP — 해외여행 중 자기 자신이 막히는 문제 회피.
- SSH 터널은 항상 유효한 우회 경로: 프록시(Caddy)를 거치지 않고 127.0.0.1 로 직접 들어온 요청
  (X-Forwarded-For 없음)은 지역 차단을 적용하지 않는다.
- GeoLite2-Country.mmdb 가 없으면 fail_open 설정에 따라 허용(+경고) 또는 차단.
"""

from __future__ import annotations

import ipaddress
import logging
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from .. import db

log = logging.getLogger("livemax.web.geo")


@lru_cache(maxsize=1)
def _reader(path: str):
    try:
        import geoip2.database  # type: ignore

        if Path(path).exists():
            return geoip2.database.Reader(path)
    except Exception as e:  # noqa: BLE001
        log.warning("GeoIP DB 로드 실패: %s", e)
    return None


def country_of(ip: str, mmdb_path: str) -> str | None:
    r = _reader(mmdb_path)
    if r is None:
        return None
    try:
        return r.country(ip).country.iso_code
    except Exception:  # noqa: BLE001
        return None


def geo_enabled(conn: sqlite3.Connection, server: dict) -> bool:
    v = db.get_setting(conn, "geo.enabled")
    if v is None:
        return bool(server["geo"]["enabled"])
    return v == "1"


def is_allowlisted(conn: sqlite3.Connection, ip: str) -> bool:
    now = time.time()
    for r in conn.execute("SELECT ip, expires FROM allow_ips").fetchall():
        if r["expires"] is not None and r["expires"] < now:
            continue
        try:
            if ipaddress.ip_address(ip) in ipaddress.ip_network(r["ip"], strict=False):
                return True
        except ValueError:
            continue
    return False


def check(conn: sqlite3.Connection, server: dict, ip: str, via_tunnel: bool) -> tuple[bool, str]:
    """(허용 여부, 사유)"""
    if via_tunnel:
        return True, "ssh-tunnel"
    if not geo_enabled(conn, server):
        return True, "geo-off"
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback:
            return True, "private"
    except ValueError:
        return False, "bad-ip"
    if is_allowlisted(conn, ip):
        return True, "allowlist"
    cc = country_of(ip, server["geo"]["mmdb_path"])
    if cc is None:
        if server["geo"]["fail_open"]:
            return True, "geo-db-missing"
        return False, "geo-db-missing"
    if cc in set(server["geo"]["allowed_countries"]):
        return True, cc
    return False, cc
