"""모바일 알림 (Pushbullet) — 문제가 생기면 휴대폰으로.

설정 > 알림 에서 켜고, Pushbullet Access Token 을 넣고, 어떤 종류를 보낼지 고른다.
토큰은 secrets/pushbullet.token (600) 에만 저장되고 API 응답·로그에는 ****ab12 로만 나간다.

Pushbullet 무료 계정은 한 달 500건까지라 폭주를 막는다:
  - 같은 내용은 dedupe_minutes(기본 10분) 안에 한 번만
  - 시간당 max_per_hour(기본 20) 건까지
보내기는 별도 스레드 큐에서 한다 — 송출·웹 응답을 절대 붙잡지 않는다.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from . import secrets
from .paths import Paths

log = logging.getLogger("livemax.notify")
API = "https://api.pushbullet.com/v2"

# (키, 이름, 설명, 기본값)
CATEGORIES: list[tuple[str, str, str, bool]] = [
    ("down", "송출 중단·오류", "1분 넘게 송출이 안 되거나, 시작에 실패하거나, 재시작 한도를 넘었을 때", True),
    ("recovered", "복구", "중단 알림을 보낸 라이브가 다시 정상 송출될 때", True),
    ("restart", "자동 재시작", "출력 정지·속도 저하·ffmpeg 종료로 자동 재시작할 때 (자주 올 수 있음)", False),
    ("timer", "타이머", "타이머로 방송이 끝나거나 새 방송이 시작될 때", True),
    ("system", "서버", "디스크 부족, 서버 재부팅 필요, LiveMax 시작(업데이트 후)", True),
    ("security", "보안", "로그인 연속 실패로 IP 가 차단될 때", True),
    ("media", "업로드 검증 실패", "올린 파일이 규격에 안 맞아 격리될 때", False),
]
DEFAULTS = {"enabled": False, "dedupe_minutes": 10, "max_per_hour": 20,
            "categories": {k: d for k, _, _, d in CATEGORIES}}


class PushError(Exception):
    pass


def token_file(paths: Paths):
    return paths.secrets / "pushbullet.token"


def read_token(paths: Paths) -> str:
    f = token_file(paths)
    return secrets.read_secret(f) if f.exists() else ""


def _post(token: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method="POST" if body is not None else "GET",
                                 headers={"Access-Token": token, "Content-Type": "application/json",
                                          "User-Agent": "LiveMax"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            raise PushError("Access Token 이 올바르지 않습니다") from None
        if e.code == 429:
            raise PushError("Pushbullet 전송 한도 초과 (무료: 월 500건)") from None
        raise PushError(f"Pushbullet 오류 HTTP {e.code}") from None
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise PushError(f"Pushbullet 에 연결할 수 없습니다 ({getattr(e, 'reason', e)})") from None


def check_token(token: str) -> str:
    """토큰 확인 — 계정 이름(또는 이메일)을 돌려준다."""
    me = _post(token, "/users/me")
    return str(me.get("name") or me.get("email") or "확인됨")


class Notifier:
    def __init__(self, paths: Paths, settings: dict):
        self.paths = paths
        self.settings = settings
        cfg = settings.setdefault("notify", {})
        for k, v in DEFAULTS.items():
            if k == "categories":
                cats = cfg.setdefault("categories", {})
                for ck, cv in v.items():
                    cats.setdefault(ck, cv)
            else:
                cfg.setdefault(k, v)
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self.recent: dict[str, float] = {}
        self.sent: deque[float] = deque(maxlen=500)
        self.last_error = ""
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def cfg(self) -> dict:
        return self.settings["notify"]

    def label(self) -> str:
        from .config import server_name

        return server_name(self.settings) or socket.gethostname()

    def wants(self, category: str) -> bool:
        return bool(self.cfg.get("enabled")) and bool(self.cfg["categories"].get(category)) and token_file(self.paths).exists()

    def send(self, category: str, title: str, body: str = "", dedupe_s: float | None = None) -> bool:
        """알림 요청 (바로 돌아옴). 실제로 큐에 넣었으면 True."""
        if not self.wants(category):
            return False
        now = time.time()
        key = f"{category}|{title}|{body}"
        window = dedupe_s if dedupe_s is not None else float(self.cfg.get("dedupe_minutes", 10)) * 60
        with self.lock:
            if now - self.recent.get(key, 0) < window:
                return False
            while self.sent and now - self.sent[0] > 3600:
                self.sent.popleft()
            if len(self.sent) >= int(self.cfg.get("max_per_hour", 20)):
                log.warning("알림 시간당 한도 초과 — 건너뜀: %s", title)
                return False
            self.recent[key] = now
            self.sent.append(now)
            if len(self.recent) > 1000:
                self.recent = {k: t for k, t in self.recent.items() if now - t < 86400}
        try:
            self.q.put_nowait((f"[{self.label()}] {title}", body))
        except queue.Full:
            return False
        self._ensure_thread()
        return True

    def test(self, token: str | None = None) -> str:
        """동기 테스트 발송 (설정 화면 '테스트' 버튼). 실패하면 PushError."""
        token = token or read_token(self.paths)
        if not token:
            raise PushError("Access Token 을 먼저 입력하세요")
        _post(token, "/pushes", {"type": "note", "title": f"[{self.label()}] LiveMax 테스트 알림",
                                  "body": "이 알림이 보이면 설정이 끝났습니다."})
        return "테스트 알림을 보냈습니다. 휴대폰을 확인하세요"

    # ---------------------------------------------------------------- 보내기 스레드
    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="notify", daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        while True:
            title, body = self.q.get()
            token = read_token(self.paths)
            if not token:
                continue
            for attempt in range(3):
                try:
                    _post(token, "/pushes", {"type": "note", "title": title, "body": body})
                    self.last_error = ""
                    log.info("알림 발송: %s", title)
                    break
                except PushError as e:
                    self.last_error = str(e)
                    log.warning("알림 실패 (%d/3): %s — %s", attempt + 1, title, e)
                    if "Token" in str(e) or "한도" in str(e):
                        break
                    time.sleep(5 * (attempt + 1))


class NullNotifier:
    """알림 기능을 쓰지 않는 곳(테스트 등)용."""

    def send(self, *a, **k) -> bool:
        return False
