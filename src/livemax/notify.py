"""모바일 알림 (Pushbullet · 텔레그램) — 문제가 생기면 휴대폰으로.

텔레그램: 봇 토큰은 secrets/telegram.token (600). 받는 대화방(chat id)은 '연결 확인'으로 자동으로 찾는다.
  보낼 알림 종류·중복 억제·시간당 한도는 Pushbullet 과 공통. 한쪽이 실패해도 다른 쪽은 보낸다.
  명령(/상태 /오늘 /라이브 N)은 저장된 대화방에서 온 것만 답한다 — 읽기 전용, 조작 명령 없음.
  텔레그램은 한 봇의 명령을 한 곳(서버)만 받을 수 있어 '이 서버에서 명령 받기'로 고른다.


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
TG_API = "https://api.telegram.org"

# (키, 이름, 설명, 기본값)
CATEGORIES: list[tuple[str, str, str, bool]] = [
    ("down", "송출 중단·오류", "1분 넘게 송출이 안 되거나, 시작에 실패하거나, 재시작 한도를 넘었을 때", True),
    ("recovered", "복구", "중단 알림을 보낸 라이브가 다시 정상 송출될 때", True),
    ("restart", "자동 재시작", "출력 정지·속도 저하·ffmpeg 종료로 자동 재시작할 때 (자주 올 수 있음)", False),
    ("timer", "타이머", "타이머로 방송이 끝나거나 새 방송이 시작될 때", True),
    ("system", "서버", "디스크 부족, 서버 재부팅 필요, LiveMax 시작(업데이트 후)", True),
    ("health", "건강검진", "매일 아침 지난 24시간 검진 결과 (시각·매일/문제 있을 때만은 아래 '건강검진'에서)", True),
    ("security", "보안", "로그인 연속 실패로 IP 가 차단될 때", True),
    ("media", "업로드 검증 실패", "올린 파일이 규격에 안 맞아 격리될 때", False),
]
DEFAULTS = {"enabled": False, "dedupe_minutes": 10, "max_per_hour": 20,
            "telegram_enabled": False, "telegram_chat_id": "", "telegram_chat_name": "", "telegram_commands": True,
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


def tg_token_file(paths: Paths):
    return paths.secrets / "telegram.token"


def read_tg_token(paths: Paths) -> str:
    f = tg_token_file(paths)
    return secrets.read_secret(f) if f.exists() else ""


def _tg(token: str, method: str, body: dict | None = None, timeout: float = 10) -> dict:
    """텔레그램 봇 API 호출. 실패하면 한국어 사유로 PushError."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(f"{TG_API}/bot{token}/{method}", data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "LiveMax"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read() or b"{}").get("description", "")
        except ValueError:
            desc = ""
        if e.code in (401, 404):
            raise PushError("텔레그램 봇 토큰이 올바르지 않습니다") from None
        if e.code == 403:
            raise PushError("봇이 대화방에 메시지를 보낼 수 없습니다 — 텔레그램에서 봇에게 '시작(Start)'을 눌렀는지, 봇을 차단하지 않았는지 확인하세요") from None
        if e.code == 409:
            raise PushError("다른 곳(다른 서버)이 이 봇의 명령을 받고 있습니다") from None
        if e.code == 429:
            raise PushError("텔레그램 전송이 너무 잦습니다 — 잠시 뒤 다시") from None
        raise PushError(f"텔레그램 오류 HTTP {e.code}{f' ({desc})' if desc else ''}") from None
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise PushError(f"텔레그램에 연결할 수 없습니다 ({getattr(e, 'reason', e)})") from None
    if not j.get("ok"):
        raise PushError(f"텔레그램 오류: {j.get('description', '알 수 없음')}")
    return j.get("result")


def tg_check(token: str) -> str:
    """봇 토큰 확인 — 봇 아이디(@...)를 돌려준다."""
    me = _tg(token, "getMe")
    return "@" + str((me or {}).get("username") or "bot")


def tg_find_chat(token: str) -> tuple[str, str]:
    """봇에게 가장 최근에 말을 건 개인 대화방을 찾는다 (사용자가 봇에서 '시작'을 누르면 생김)."""
    ups = _tg(token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]}) or []
    for u in reversed(ups):
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private" and chat.get("id"):
            name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or "나"
            return str(chat["id"]), str(name)
    raise PushError("봇과의 대화방을 찾지 못했습니다 — 휴대폰 텔레그램에서 만든 봇을 열고 '시작(Start)'을 누르거나 아무 말이나 보낸 뒤 다시 누르세요")


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
        self.tg_last_error = ""
        self.answer = None  # 웹(API)이 끼워 넣는다: 명령 글자 → 답장 글자
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def cfg(self) -> dict:
        return self.settings["notify"]

    def label(self) -> str:
        from .config import server_name

        return server_name(self.settings) or socket.gethostname()

    def pb_on(self) -> bool:
        return bool(self.cfg.get("enabled")) and token_file(self.paths).exists()

    def tg_on(self) -> bool:
        return bool(self.cfg.get("telegram_enabled")) and bool(self.cfg.get("telegram_chat_id")) and tg_token_file(self.paths).exists()

    def wants(self, category: str) -> bool:
        return bool(self.cfg["categories"].get(category)) and (self.pb_on() or self.tg_on())

    def send(self, category: str, title: str, body: str = "", dedupe_s: float | None = None) -> bool:
        """알림 요청 (바로 돌아옴). 실제로 큐에 넣었으면 True. 켜져 있는 곳(Pushbullet·텔레그램) 모두로."""
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

    def tg_send(self, text: str, chat_id: str | None = None, token: str | None = None) -> None:
        token = token or read_tg_token(self.paths)
        chat_id = chat_id or str(self.cfg.get("telegram_chat_id") or "")
        if not token or not chat_id:
            raise PushError("텔레그램 봇 토큰과 대화방 연결을 먼저 하세요")
        _tg(token, "sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})

    def tg_test(self) -> str:
        self.tg_send(f"[{self.label()}] LiveMax 텔레그램 테스트 알림\n이 메시지가 보이면 설정이 끝났습니다.")
        return "텔레그램으로 테스트 알림을 보냈습니다. 휴대폰을 확인하세요"

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
            # 텔레그램 먼저 (보통 1~2초 안에 도착) — 한쪽이 실패해도 다른 쪽은 보낸다
            if self.tg_on():
                self._deliver("텔레그램", lambda: self.tg_send(f"{title}\n{body}" if body else title), "tg_last_error", title)
            if self.pb_on():
                token = read_token(self.paths)
                self._deliver("Pushbullet", lambda: _post(token, "/pushes", {"type": "note", "title": title, "body": body}),
                              "last_error", title)

    def _deliver(self, name: str, fn, err_attr: str, title: str) -> None:
        for attempt in range(3):
            try:
                fn()
                setattr(self, err_attr, "")
                log.info("알림 발송(%s): %s", name, title)
                return
            except PushError as e:
                setattr(self, err_attr, str(e))
                log.warning("알림 실패(%s %d/3): %s — %s", name, attempt + 1, title, e)
                if "토큰" in str(e) or "Token" in str(e) or "한도" in str(e) or "시작(Start)" in str(e):
                    return
                time.sleep(5 * (attempt + 1))

    # ---------------------------------------------------------------- 텔레그램 명령 (읽기 전용)
    def run_bot(self, stop: threading.Event) -> None:
        """저장된 대화방에서 온 /상태 /오늘 /라이브 N 에 답한다. 꺼져 있으면 30초마다 다시 확인만."""
        offset = None
        while not stop.is_set():
            if not (self.tg_on() and self.cfg.get("telegram_commands", True) and self.answer):
                offset = None
                stop.wait(30)
                continue
            token = read_tg_token(self.paths)
            try:
                body = {"timeout": 50, "allowed_updates": ["message"]}
                if offset is not None:
                    body["offset"] = offset
                ups = _tg(token, "getUpdates", body, timeout=60) or []
            except PushError as e:
                self.tg_last_error = str(e) if "다른 곳" in str(e) else self.tg_last_error
                log.warning("텔레그램 명령 받기 실패: %s", e)
                stop.wait(60)
                continue
            for u in ups:
                offset = int(u.get("update_id", 0)) + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id")) != str(self.cfg.get("telegram_chat_id")):
                    continue  # 다른 사람이 봇에 말을 걸어도 무시
                text = str(m.get("text") or "").strip()
                if not text:
                    continue
                try:
                    reply = self.answer(text)
                except Exception:  # noqa: BLE001
                    log.exception("텔레그램 명령 처리 실패: %s", text)
                    reply = "처리 중 오류가 났습니다 — 사이트 로그를 확인하세요"
                if reply:
                    try:
                        self.tg_send(reply, token=token)
                    except PushError as e:
                        log.warning("텔레그램 답장 실패: %s", e)


class NullNotifier:
    """알림 기능을 쓰지 않는 곳(테스트 등)용."""

    def send(self, *a, **k) -> bool:
        return False
