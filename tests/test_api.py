"""웹 API: 로그인·비밀번호 강제 변경·차단, 라이브 생성(키 마스킹), 미디어 폴더, 이어 올리기, 재부팅 표시."""

import time
import warnings

import pytest

warnings.filterwarnings("ignore")
ORIGIN = {"origin": "https://testserver"}
NEW = "Correct-Horse-Battery-9"


class _Collector:
    latest, per_live = {}, {}


@pytest.fixture
def client(paths, settings):
    from fastapi.testclient import TestClient

    from livemax.supervisor import Supervisor
    from livemax.web.api import create_app

    class Core:
        pass

    core = Core()
    core.paths, core.settings, core.version, core.started_at = paths, settings, "test", time.time()
    core.supervisor = Supervisor(paths, settings)
    core.collector = _Collector()
    core.cleanup_async = lambda **k: True
    app = create_app(core)
    c = TestClient(app, base_url="https://testserver")
    pw = (paths.secrets / "admin.initial_password").read_text().strip()
    c.core = core
    c.initial_pw = pw
    return c


def login(c, pw):
    return c.post("/api/login", json={"username": "admin", "password": pw}, headers=ORIGIN)


def test_forced_password_change_then_access(client):
    assert client.get("/api/overview").status_code == 401
    r = login(client, client.initial_pw)
    assert r.status_code == 200 and r.json()["must_change"] is True
    assert "HttpOnly" in r.headers["set-cookie"] and "Secure" in r.headers["set-cookie"]
    assert client.get("/api/overview").status_code == 428
    assert client.post("/api/password", json={"current": client.initial_pw, "new": "short"}, headers=ORIGIN).status_code == 400
    assert client.post("/api/password", json={"current": client.initial_pw, "new": NEW}, headers=ORIGIN).status_code == 200
    assert client.get("/api/overview").status_code == 200


def test_csrf_and_lockout(client):
    assert client.post("/api/login", json={"username": "admin", "password": "x"}, headers={"origin": "https://evil"}).status_code == 403
    for _ in range(5):
        login(client, "wrong")
    assert login(client, client.initial_pw).status_code == 429


def _ready(client):
    login(client, client.initial_pw)
    client.post("/api/password", json={"current": client.initial_pw, "new": NEW}, headers=ORIGIN)
    for kind, name in (("video", "Sax After Six"), ("audio", "재즈-색소폰")):
        assert client.post(f"/api/media/{kind}", json={"name": name}, headers=ORIGIN).json()["ok"]


def test_create_live_masks_key(client):
    _ready(client)
    r = client.post("/api/lives", headers=ORIGIN, json={"name": "재즈", "stream_key": "fake-0000-0000-0000-9z9z",
                                                       "video_folders": ["Sax After Six"], "audio_folders": ["재즈-색소폰"], "apply": "later"})
    assert r.json() == {"ok": True, "id": "live1"}
    ov = client.get("/api/overview").text
    assert "fake-0000" not in ov and "****9z9z" in ov
    assert client.get("/api/media/video").json()["folders"][0]["used_by"] == ["live1"]
    # 사용 중인 폴더는 이름 변경·삭제 불가
    assert client.delete("/api/media/video/Sax After Six", headers=ORIGIN).status_code == 400


def test_create_live_requires_folders_and_key(client):
    _ready(client)
    r = client.post("/api/lives", headers=ORIGIN, json={"name": "x", "stream_key": "k", "video_folders": [], "audio_folders": ["재즈-색소폰"]})
    assert r.status_code == 400 and "영상 폴더" in r.json()["error"]
    r = client.post("/api/lives", headers=ORIGIN, json={"name": "x", "video_folders": ["Sax After Six"], "audio_folders": ["재즈-색소폰"]})
    assert r.status_code == 400 and "스트림 키" in r.json()["error"]


def test_resumable_upload(client):
    _ready(client)
    data = b"0123456789" * 1000
    b = client.post("/api/uploads", headers=ORIGIN, json={"kind": "audio", "folder": "재즈-색소폰", "name": "a.m4a", "size": len(data)}).json()
    uid = b["id"]
    assert client.put(f"/api/uploads/{uid}?offset=0", content=data[:4000], headers=ORIGIN).json()["offset"] == 4000
    # 연결이 끊긴 뒤 다시 시작하면 서버가 가진 지점을 알려준다
    again = client.post("/api/uploads", headers=ORIGIN, json={"kind": "audio", "folder": "재즈-색소폰", "name": "a.m4a", "size": len(data)}).json()
    assert again == {"id": uid, "offset": 4000}
    assert client.put(f"/api/uploads/{uid}?offset=0", content=b"zz", headers=ORIGIN).status_code == 409  # 잘못된 지점 거부
    client.put(f"/api/uploads/{uid}?offset=4000", content=data[4000:], headers=ORIGIN)
    assert client.post(f"/api/uploads/{uid}/complete", headers=ORIGIN).json()["ok"]
    staged = client.core.paths.incoming / "audio" / "재즈-색소폰" / "a.m4a"
    assert staged.read_bytes() == data  # 방송 폴더가 아니라 스테이징으로 (DESIGN 11-3)


def test_reboot_flag_shown(client):
    _ready(client)
    assert client.get("/api/overview").json()["reboot_required"] is False
    flag = client.core.paths.state / "host" / "reboot-required"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("*** System restart required ***")
    ov = client.get("/api/overview").json()
    assert ov["reboot_required"] is True and any("재부팅" in a["message"] for a in ov["alerts"])


def test_spa_served(client):
    assert "LiveMax" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
