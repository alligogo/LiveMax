"""링크로 가져오기: 공유 링크 변환, 내부 주소 차단, 실제 받기(숨김 임시 파일 → 원래 이름), 이름 겹침 거부, 취소."""

import http.server
import threading
import time
import urllib.parse

import pytest

from livemax import fetcher as fx


def test_share_links_become_direct_download():
    g = fx.normalize_url("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing")
    assert g == "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOp&export=download&confirm=t"
    d = fx.normalize_url("https://www.dropbox.com/scl/fi/xyz/song.m4a?rlkey=abc&dl=0")
    assert "dl=1" in d and "dl=0" not in d and "rlkey=abc" in d
    with pytest.raises(fx.FetchError):
        fx.normalize_url("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/x.mp4", "http://10.0.0.5/x.mp4", "http://192.168.1.2/a.m4a",
                                 "file:///etc/passwd", "ftp://example.com/a.mp4"])
def test_internal_or_odd_addresses_refused(url):
    with pytest.raises(fx.FetchError):
        fx._check_host(url, allow_private=False)


class _H(http.server.BaseHTTPRequestHandler):
    body = b"x" * (3 * 1024 * 1024)

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        if q.path == "/page":
            data = b"<html>login</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%EC%9E%AC%EC%A6%88%20%EA%B3%A1.m4a")
        self.end_headers()
        try:
            for i in range(0, len(self.body), 256 * 1024):
                self.wfile.write(self.body[i:i + 256 * 1024])
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _run(f: fx.Fetcher, timeout=20):
    stop = threading.Event()
    t = threading.Thread(target=f.run, args=(stop,), daemon=True)
    t.start()
    end = time.time() + timeout
    while time.time() < end and any(j["state"] in ("wait", "run") for j in f.list()):
        time.sleep(0.1)
    stop.set()
    f.wake.set()
    t.join(5)
    return f.list()


def test_download_lands_in_incoming_with_real_name(paths, settings, server):
    settings["fetch"] = {"mb_per_s": 100, "allow_private": True}
    (paths.audio / "재즈").mkdir(parents=True)
    f = fx.Fetcher(paths, settings)
    f.add("audio", "재즈", [f"{server}/dl?id=1", f"{server}/page"])
    jobs = _run(f)
    assert jobs[0]["state"] == "done" and jobs[0]["name"] == "재즈 곡.m4a"
    got = paths.incoming / "audio" / "재즈" / "재즈 곡.m4a"
    assert got.stat().st_size == len(_H.body)
    assert not list((paths.incoming / "audio" / "재즈").glob(".*.part"))  # 임시 파일 남지 않음
    assert jobs[1]["state"] == "err" and "웹페이지" in jobs[1]["error"]
    # 같은 이름은 거부
    f.add("audio", "재즈", [f"{server}/dl?id=2"])
    assert _run(f)[-1]["state"] == "err"


def test_cancel_while_downloading_leaves_nothing(paths, settings, server):
    settings["fetch"] = {"mb_per_s": 1, "allow_private": True}  # 천천히 받게
    (paths.video / "v").mkdir(parents=True)
    _H.body = b"y" * (3 * 1024 * 1024)
    f = fx.Fetcher(paths, settings)
    stop = threading.Event()
    t = threading.Thread(target=f.run, args=(stop,), daemon=True)
    t.start()
    # 이름 확장자가 .m4a 라 비디오 폴더에서는 거부되므로 오디오로
    (paths.audio / "a").mkdir(parents=True)
    jid = f.add("audio", "a", [f"{server}/x"])[0]["id"]
    for _ in range(50):
        if f.list()[0]["got"] > 0:
            break
        time.sleep(0.05)
    assert f.cancel(jid)
    for _ in range(100):
        if f.list()[0]["state"] == "cancel" and not list((paths.incoming / "audio" / "a").glob("*")) :
            break
        time.sleep(0.05)
    stop.set()
    f.wake.set()
    t.join(5)
    assert f.list()[0]["state"] == "cancel"
    assert not [p for p in (paths.incoming / "audio" / "a").glob("*")]
