"""채널 (v2.4) — 한 서버에서 여러 유튜브 채널을 운영할 때 라이브를 채널별로 묶는다.

채널 = 이름 · 설명 · 주소(유튜브 채널 URL) · 로고 · 스트림 키 여러 개.
스트림 키는 채널 메뉴에서만 등록한다. 라이브는 "채널 + 그 채널의 키 하나"를 고른다.
한 키는 한 라이브만 쓸 수 있다 (같은 키로 두 라이브가 동시에 송출하면 유튜브에서 서로 끊긴다).

저장 위치
  config/channels/<id>.yaml   이름·설명·주소·키 목록(키 이름과 등록 시각만 — 키 값은 없음)
  config/channels/<id>.logo   로고 (PNG/JPEG/WEBP, 1MB 이하)
  secrets/channels/<id>-<kid>.key   키 값 (600). 화면·API·로그에는 ****ab12 로만 나간다.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import secrets
from .config import ConfigError, Live, LiveStore, _atomic_write
from .paths import Paths

log = logging.getLogger("livemax.channels")
CH_ID_RE = re.compile(r"^ch(\d{1,4})$")
KEY_ID_RE = re.compile(r"^k(\d{1,4})$")
KEY_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{8,200}$")
URL_RE = re.compile(r"^https?://[^\s<>\"'`]{3,300}$")
LOGO_MAX = 1_000_000
LOGO_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def logo_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@dataclass
class Channel:
    id: str
    name: str
    description: str = ""
    url: str = ""
    logo: str = ""          # png | jpeg | webp | ""
    logo_ver: int = 0       # 로고를 바꿀 때마다 +1 (브라우저 캐시 갱신용)
    keys: list[dict] = field(default_factory=list)   # [{id, label, created_at}]
    created_at: float = 0.0

    @property
    def number(self) -> int:
        return int(CH_ID_RE.match(self.id).group(1))

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "url": self.url,
                "logo": self.logo, "logo_ver": self.logo_ver, "keys": [dict(k) for k in self.keys],
                "created_at": self.created_at}

    def validate(self) -> None:
        if not CH_ID_RE.match(self.id):
            raise ConfigError("채널 ID 형식 오류")
        if not self.name.strip() or len(self.name) > 40:
            raise ConfigError("채널명은 1~40자")
        if len(self.description) > 120:
            raise ConfigError("채널 설명은 120자 이하")
        if self.url and not URL_RE.match(self.url):
            raise ConfigError("채널 주소는 https:// 로 시작하는 주소여야 합니다")
        for k in self.keys:
            if not KEY_ID_RE.match(str(k.get("id", ""))) or not str(k.get("label", "")).strip():
                raise ConfigError("스트림 키 정보 오류")

    def key(self, kid: str) -> dict | None:
        return next((k for k in self.keys if k.get("id") == kid), None)


def _from_dict(d: dict) -> Channel:
    known = Channel.__dataclass_fields__.keys()
    ch = Channel(**{k: v for k, v in (d or {}).items() if k in known})
    ch.keys = [{"id": str(k.get("id")), "label": str(k.get("label", "")), "created_at": float(k.get("created_at", 0))}
               for k in (ch.keys or [])]
    return ch


class ChannelStore:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.dir = paths.config / "channels"
        self.key_dir = paths.secrets / "channels"

    def _file(self, cid: str) -> Path:
        return self.dir / f"{cid}.yaml"

    def logo_path(self, cid: str) -> Path:
        return self.dir / f"{cid}.logo"

    def key_path(self, cid: str, kid: str) -> Path:
        return self.key_dir / f"{cid}-{kid}.key"

    def all(self) -> list[Channel]:
        out = []
        if self.dir.is_dir():
            for p in self.dir.glob("ch*.yaml"):
                try:
                    out.append(_from_dict(yaml.safe_load(p.read_text(encoding="utf-8"))))
                except Exception:  # noqa: BLE001
                    continue
        return sorted(out, key=lambda c: c.number)

    def get(self, cid: str) -> Channel | None:
        if not CH_ID_RE.match(cid or ""):
            return None
        p = self._file(cid)
        return _from_dict(yaml.safe_load(p.read_text(encoding="utf-8"))) if p.exists() else None

    def new(self, name: str) -> Channel:
        used = {c.number for c in self.all()}
        n = next(i for i in range(1, 10000) if i not in used)
        return Channel(id=f"ch{n}", name=name.strip(), created_at=time.time())

    def save(self, ch: Channel) -> None:
        ch.name, ch.description, ch.url = ch.name.strip(), ch.description.strip(), ch.url.strip()
        ch.validate()
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self._file(ch.id), yaml.safe_dump(ch.to_dict(), allow_unicode=True, sort_keys=False))

    def delete(self, cid: str) -> None:
        ch = self.get(cid)
        if ch:
            for k in ch.keys:
                self.key_path(cid, k["id"]).unlink(missing_ok=True)
        self.logo_path(cid).unlink(missing_ok=True)
        self._file(cid).unlink(missing_ok=True)

    # ---------------------------------------------------------------- 키
    def all_key_values(self) -> dict[str, tuple[str, str]]:
        """키 값 → (채널, 키 ID). 같은 키를 두 번 등록하지 않게 확인할 때만 쓴다."""
        out = {}
        for ch in self.all():
            for k in ch.keys:
                p = self.key_path(ch.id, k["id"])
                if p.exists():
                    out[secrets.read_secret(p)] = (ch.id, k["id"])
        return out

    def add_key(self, ch: Channel, label: str, value: str) -> str:
        value, label = value.strip(), label.strip()[:30]
        if not label:
            raise ConfigError("키 이름을 입력하세요 (예: 메인 방송)")
        if not KEY_VALUE_RE.match(value):
            raise ConfigError("스트림 키 형식이 올바르지 않습니다 (영문·숫자·- 만, 8자 이상)")
        dup = self.all_key_values().get(value)
        if dup:
            other = self.get(dup[0])
            raise ConfigError(f"이미 등록된 키입니다 ({other.name if other else dup[0]})")
        used = {int(KEY_ID_RE.match(k["id"]).group(1)) for k in ch.keys}
        kid = f"k{next(i for i in range(1, 10000) if i not in used)}"
        secrets.write_secret(self.key_path(ch.id, kid), value)
        ch.keys.append({"id": kid, "label": label, "created_at": time.time()})
        self.save(ch)
        return kid

    def remove_key(self, ch: Channel, kid: str) -> None:
        ch.keys = [k for k in ch.keys if k["id"] != kid]
        self.save(ch)
        self.key_path(ch.id, kid).unlink(missing_ok=True)

    def masked(self, cid: str, kid: str) -> str:
        p = self.key_path(cid, kid)
        return secrets.mask(p.read_text().strip()) if p.exists() else ""


def live_key_path(paths: Paths, lv: Live) -> Path:
    """라이브가 쓰는 스트림 키 파일. 채널 키를 골랐으면 그것, 아니면 v2.3 이전 방식(라이브별 키)."""
    if lv.channel_id and lv.key_id:
        return ChannelStore(paths).key_path(lv.channel_id, lv.key_id)
    return paths.stream_key(lv.id)


def key_users(lives: list[Live]) -> dict[tuple[str, str], Live]:
    return {(lv.channel_id, lv.key_id): lv for lv in lives if lv.channel_id and lv.key_id}


def migrate_legacy(paths: Paths, settings: dict) -> str | None:
    """v2.3 이전 라이브(라이브마다 키 파일)를 '내 채널' 하나로 옮긴다. 채널이 하나도 없을 때 1번만."""
    cs, ls = ChannelStore(paths), LiveStore(paths, settings)
    lives = [lv for lv in ls.all() if not lv.channel_id and paths.stream_key(lv.id).exists()]
    if cs.all() or not lives:
        return None
    ch = cs.new("내 채널")
    ch.description = "기존 라이브에서 옮겨 온 채널 — 채널 메뉴에서 이름·로고·주소를 바꾸세요"
    cs.save(ch)
    by_value: dict[str, str] = {}
    for lv in lives:
        value = secrets.read_secret(paths.stream_key(lv.id))
        if value not in by_value:
            try:
                by_value[value] = cs.add_key(ch, lv.name[:30] or lv.id, value)
            except ConfigError as e:
                log.warning("%s 키 이전 실패: %s", lv.id, e)
                continue
            ch = cs.get(ch.id)
        if any(o.channel_id == ch.id and o.key_id == by_value[value] for o in ls.all()):
            log.warning("%s: 같은 키를 쓰는 라이브가 이미 있어 채널 키를 연결하지 않음 (라이브 설정에서 고르세요)", lv.id)
            continue
        lv.channel_id, lv.key_id = ch.id, by_value[value]
        ls.save(lv)
        paths.stream_key(lv.id).unlink(missing_ok=True)
    msg = f"기존 라이브 {len(lives)}개의 스트림 키를 '{ch.name}' 채널로 옮겼습니다"
    log.info(msg)
    return msg
