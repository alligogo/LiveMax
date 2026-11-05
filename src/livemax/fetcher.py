"""링크로 가져오기 — 구글 드라이브·드롭박스·일반 다운로드 주소를 서버가 직접 받아 _incoming 에 넣는다.

왜: 집 → 해외 서버 업로드는 느리다(국제 회선, 왕복 0.3초). 파일을 구글 드라이브 등에 먼저 올리고 링크만 주면
서버가 데이터센터 회선으로 몇 초 만에 받는다. 받은 파일은 업로드와 똑같이 ingest 가 검사해 방송 폴더에 넣는다.

안전:
  - http/https 만. 서버 안쪽 주소(127.x, 10.x, 192.168.x 등)로는 가지 않는다 (리다이렉트 포함) — 내부 서비스 보호.
  - 받는 중에는 숨김 파일(.이름.part)로 쓰고, 다 받은 뒤에만 원래 이름으로 → ingest 가 반쪽 파일을 집지 않는다.
  - 속도 상한(기본 초당 20MB)·낮은 우선순위·한 번에 하나씩 — 송출 CPU 를 뺏지 않게.
  - 이름이 겹치면(방송 폴더·검증 대기) 받기 전에 거부.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .paths import Paths

log = logging.getLogger("livemax.fetcher")
EXTS = {"video": (".mp4", ".mkv"), "audio": (".m4a", ".aac")}
UA = "Mozilla/5.0 (LiveMax link fetch)"


class FetchError(Exception):
    pass


def normalize_url(url: str) -> str:
    """공유 링크 → 바로 받는 주소."""
    url = url.strip()
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    if host in ("drive.google.com", "docs.google.com"):
        m = re.search(r"/file/d/([\w-]{10,})", u.path) or re.search(r"[?&]id=([\w-]{10,})", url)
        if "/folders/" in u.path:
            raise FetchError("구글 드라이브 '폴더' 링크는 안 됩니다 — 파일마다 공유 링크를 넣으세요")
        if not m:
            raise FetchError("구글 드라이브 파일 링크가 아닙니다")
        # 큰 파일의 '바이러스 검사 못 함' 확인 화면을 건너뛰는 주소
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    if host.endswith("dropbox.com"):
        q = urllib.parse.parse_qs(u.query)
        q.pop("dl", None)
        q["dl"] = ["1"]
        return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q, doseq=True)))
    return url


def _check_host(url: str, allow_private: bool) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("http(s) 링크만 가능합니다")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError as e:
        raise FetchError(f"주소를 찾을 수 없음: {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise FetchError("서버 내부 주소로는 가져올 수 없습니다")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _filename(resp, url: str) -> str:
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1))
    else:
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
        name = m.group(1).strip() if m else urllib.parse.unquote(Path(urllib.parse.urlparse(resp.geturl() or url).path).name)
    return Path(name.replace("\\", "/")).name.strip()


@dataclass
class Job:
    id: int
    kind: str
    folder: str
    url: str
    state: str = "wait"  # wait | run | done | err | cancel
    name: str = ""
    total: int = 0
    got: int = 0
    speed: float = 0.0
    error: str = ""
    created: float = field(default_factory=time.time)

    def view(self) -> dict:
        u = urllib.parse.urlparse(self.url)
        return {"id": self.id, "kind": self.kind, "folder": self.folder, "state": self.state, "name": self.name,
                "source": u.hostname or "", "total": self.total, "got": self.got, "speed": round(self.speed),
                "error": self.error}


class Fetcher:
    def __init__(self, paths: Paths, settings: dict):
        self.paths = paths
        self.settings = settings
        self.jobs: dict[int, Job] = {}
        self.queue: deque[Job] = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.next_id = 1

    # ---- 웹에서
    def add(self, kind: str, folder: str, urls: list[str]) -> list[dict]:
        out = []
        with self.lock:
            for raw in urls:
                raw = raw.strip()
                if not raw:
                    continue
                job = Job(self.next_id, kind, folder, raw)
                self.next_id += 1
                self.jobs[job.id] = job
                self.queue.append(job)
                out.append(job.view())
            # 끝난 지 1시간 넘은 기록은 정리
            for jid in [j.id for j in self.jobs.values() if j.state in ("done", "err", "cancel") and time.time() - j.created > 3600]:
                self.jobs.pop(jid, None)
        self.wake.set()
        return out

    def list(self) -> list[dict]:
        with self.lock:
            return [j.view() for j in sorted(self.jobs.values(), key=lambda j: j.id)]

    def cancel(self, jid: int) -> bool:
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j.state not in ("wait", "run"):
                return False
            if j.state == "wait":
                try:
                    self.queue.remove(j)
                except ValueError:
                    pass
                j.state = "cancel"
            else:
                j.state = "cancel"  # 받는 쪽이 다음 조각에서 멈추고 임시 파일을 지운다
            return True

    # ---- 뒤에서 (한 번에 하나씩)
    def run(self, stop: threading.Event) -> None:
        try:
            os.nice(15)
        except OSError:
            pass
        while not stop.is_set():
            self.wake.wait(5)
            self.wake.clear()
            while not stop.is_set():
                with self.lock:
                    job = self.queue.popleft() if self.queue else None
                    if job:
                        job.state = "run"
                if not job:
                    break
                try:
                    self._download(job)
                except FetchError as e:
                    job.state, job.error = "err", str(e)
                    log.warning("링크 가져오기 실패 (%s): %s", job.folder, e)
                except Exception as e:  # noqa: BLE001
                    job.state, job.error = "err", f"받기 실패: {type(e).__name__}"
                    log.exception("링크 가져오기 오류")

    def _download(self, job: Job) -> None:
        cfg = self.settings.get("fetch", {})
        allow_private = bool(cfg.get("allow_private", False))
        url = normalize_url(job.url)
        _check_host(url, allow_private)
        opener = urllib.request.build_opener(_SafeRedirect(allow_private))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with opener.open(req, timeout=30) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise FetchError("파일이 아니라 웹페이지가 왔습니다 — 공유 설정을 '링크가 있는 모든 사용자'로 했는지 확인하세요")
            name = _filename(resp, url)
            if not name or name.startswith(".") or not name.lower().endswith(EXTS[job.kind]):
                raise FetchError(f"파일 이름 '{name or '?'}' — {', '.join(EXTS[job.kind])} 파일만 됩니다")
            job.name = name
            total = int(resp.headers.get("Content-Length") or 0)
            job.total = total
            max_b = int(self.settings["dashboard"]["max_upload_mb"]) * 1024 * 1024
            if total and total > max_b:
                raise FetchError("파일이 너무 큽니다")
            for d in (self.paths.kind_root(job.kind) / job.folder, self.paths.incoming / job.kind / job.folder):
                if d.is_dir() and any(f.name.lower() == name.lower() for f in d.iterdir() if f.is_file()):
                    raise FetchError(f"'{name}' 과(와) 같은 이름의 파일이 이미 있습니다")
            dest_dir = self.paths.incoming / job.kind / job.folder
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest_dir / f".{name}.part"
            rate = float(cfg.get("mb_per_s", 20)) * 1024 * 1024
            t0 = time.monotonic()
            try:
                with open(tmp, "wb") as f:
                    while True:
                        if job.state == "cancel":
                            raise FetchError("취소됨")
                        buf = resp.read(1024 * 1024)
                        if not buf:
                            break
                        f.write(buf)
                        job.got += len(buf)
                        if job.got > max_b:
                            raise FetchError("파일이 너무 큽니다")
                        el = time.monotonic() - t0
                        job.speed = job.got / max(el, 0.001)
                        ahead = job.got / rate - el  # 속도 상한
                        if ahead > 0.02:
                            time.sleep(ahead)
                if total and job.got != total:
                    raise FetchError(f"덜 받았습니다 ({job.got}/{total} 바이트)")
                final = dest_dir / name
                os.link(tmp, final)  # 같은 이름이 생겼으면 실패 (덮어쓰지 않음)
            except FileExistsError as e:
                raise FetchError(f"'{name}' 과(와) 같은 이름의 파일이 이미 있습니다") from e
            except FetchError as e:
                if job.state == "cancel":
                    job.error = ""
                    return
                raise e
            finally:
                tmp.unlink(missing_ok=True)
        job.state = "done"
        log.info("링크로 가져옴 [%s/%s] %s (%.1f MB, %.1f MB/s)", job.kind, job.folder, name, job.got / 1e6, job.speed / 1e6)
