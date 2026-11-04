"""새 버전 확인 — 설정 화면에 '최신 버전입니다 / 새 버전 있음: abc1234' 을 보여 주기 위해.

GHCR(ghcr.io/alligogo/livemax)의 `latest` 태그와 지금 돌고 있는 버전 태그(커밋 sha)의 매니페스트
digest 를 비교한다. 같으면 최신, 다르면 새 버전이 있다. 새 버전 이름은 sha 태그들 중 latest 와
digest 가 같은 것으로 찾는다 (sha 태그는 바뀌지 않으므로 결과를 기억해 둔다).
공개 이미지라 익명 토큰으로 충분하다. 30분마다 백그라운드에서만 확인 (웹 요청을 기다리게 하지 않음).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("livemax.updatecheck")
REPO = "alligogo/livemax"
REG = "https://ghcr.io"
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _get(url: str, headers: dict | None = None, method: str = "GET", timeout: float = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "LiveMax", **(headers or {})}, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _token() -> str:
    with _get(f"{REG}/token?scope=repository:{REPO}:pull") as r:
        return json.loads(r.read())["token"]


def _digest(tok: str, ref: str) -> str | None:
    try:
        with _get(f"{REG}/v2/{REPO}/manifests/{ref}", {"Authorization": f"Bearer {tok}", "Accept": ACCEPT},
                  method="HEAD") as r:
            return r.headers.get("Docker-Content-Digest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


class UpdateChecker:
    def __init__(self, current: str, interval_s: float = 1800):
        self.current = current
        self.interval = interval_s
        self.state: dict = {"status": "unknown", "latest": None, "checked_at": None, "error": ""}
        self._names: dict[str, str] = {}  # digest → sha 태그 (불변이라 기억)
        self._lock = threading.Lock()

    def check_once(self) -> dict:
        if not SHA_RE.match(self.current or ""):
            self.state = {"status": "dev", "latest": None, "checked_at": time.time(), "error": ""}
            return self.state
        try:
            tok = _token()
            latest = _digest(tok, "latest")
            mine = _digest(tok, self.current)
            if latest and latest == mine:
                st = {"status": "latest", "latest": self.current}
            else:
                st = {"status": "available", "latest": self._name_of(tok, latest)}
            self.state = {**st, "checked_at": time.time(), "error": ""}
        except Exception as e:  # noqa: BLE001 — 확인 실패는 화면에 '확인 못 함'으로만
            log.info("새 버전 확인 실패: %s", e)
            self.state = {**self.state, "status": "error", "checked_at": time.time(), "error": str(e)[:120]}
        return self.state

    def _name_of(self, tok: str, digest: str | None) -> str | None:
        if not digest:
            return None
        if digest in self._names:
            return self._names[digest]
        try:
            with _get(f"{REG}/v2/{REPO}/tags/list?n=1000", {"Authorization": f"Bearer {tok}"}) as r:
                tags = [t for t in json.loads(r.read()).get("tags", []) if SHA_RE.match(t)]
        except Exception:  # noqa: BLE001
            return None
        for t in reversed(tags):
            if t in self._names.values():
                continue
            d = _digest(tok, t)
            if d:
                self._names[d] = t
            if d == digest:
                return t
        return None

    def run(self, stop: threading.Event) -> None:
        if stop.wait(60):
            return
        while True:
            self.check_once()
            if stop.wait(self.interval):
                return

    def refresh_async(self) -> None:
        """설정 화면을 열었을 때 10분 넘은 결과면 뒤에서 다시 확인."""
        ts = self.state.get("checked_at") or 0
        if time.time() - ts < 600 or not self._lock.acquire(blocking=False):
            return

        def go():
            try:
                self.check_once()
            finally:
                self._lock.release()

        threading.Thread(target=go, name="updatecheck", daemon=True).start()
