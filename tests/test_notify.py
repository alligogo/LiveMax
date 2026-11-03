"""모바일 알림(Pushbullet): 종류별 켜기/끄기, 중복 억제, 시간당 한도, 토큰은 응답에 평문으로 안 나감."""

import time

import pytest

from livemax import notify, secrets


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "_post", lambda token, path, body=None, timeout=10: out.append((token, path, body)) or {"name": "tester"})
    return out


def _wait(sent, n):
    for _ in range(50):
        if len(sent) >= n:
            return
        time.sleep(0.05)


def test_categories_dedupe_and_limit(paths, settings, sent):
    n = notify.Notifier(paths, settings)
    assert n.send("down", "x") is False  # 꺼져 있음
    settings["notify"]["enabled"] = True
    assert n.send("down", "x") is False  # 토큰 없음
    secrets.write_secret(notify.token_file(paths), "o.faketoken000000000000")
    assert n.send("down", "live1 중단", "body") is True
    assert n.send("down", "live1 중단", "body") is False  # 10분 안 같은 내용
    assert n.send("restart", "자동 재시작") is False       # 기본 꺼짐
    _wait(sent, 1)
    assert sent[0][1] == "/pushes" and sent[0][2]["title"].endswith("live1 중단") and sent[0][2]["type"] == "note"
    settings["notify"]["max_per_hour"] = 2
    assert n.send("down", "b") is True
    assert n.send("down", "c") is False  # 시간당 한도


def test_notify_api(client, sent, monkeypatch):
    from test_api import ORIGIN, _ready

    _ready(client)
    r = client.put("/api/system/notify", headers=ORIGIN, json={"enabled": True})
    assert r.status_code == 400  # 토큰 먼저
    r = client.put("/api/system/notify", headers=ORIGIN, json={"token": "o.faketoken0000000000001a2b", "enabled": True,
                                                                "categories": {"restart": True, "bogus": True}})
    assert r.json()["ok"] and "tester" in r.json()["message"]
    sysd = client.get("/api/system").json()["notify"]
    assert sysd["enabled"] and sysd["categories"]["restart"] and "bogus" not in sysd["categories"]
    assert sysd["token_masked"] == "****1a2b" and "faketoken" not in client.get("/api/system").text
    assert client.post("/api/system/notify/test", headers=ORIGIN).json()["ok"]
    assert any(p == "/pushes" for _, p, _ in sent)
    client.put("/api/system/notify", headers=ORIGIN, json={"clear_token": True, "enabled": False})
    assert client.get("/api/system").json()["notify"]["token_masked"] == ""


def test_bad_token_rejected(client, monkeypatch):
    from test_api import ORIGIN, _ready

    def boom(*a, **k):
        raise notify.PushError("Access Token 이 올바르지 않습니다")
    monkeypatch.setattr(notify, "_post", boom)
    _ready(client)
    r = client.put("/api/system/notify", headers=ORIGIN, json={"token": "o.wrong"})
    assert r.status_code == 400 and "Token" in r.json()["error"]
    assert not notify.token_file(client.core.paths).exists()


def test_supervisor_down_and_recovered(paths, settings):
    from livemax.supervisor import LiveProc, Supervisor
    from conftest import make_live

    sup = Supervisor(paths, settings)
    got = []
    sup.notify = lambda cat, title, body="", **k: got.append((cat, title))
    sup.store.save(make_live())
    p = LiveProc(sup, "live1")
    p.desired = True
    p.st.update(state="error", message="오디오 준비 안 됨")
    now = time.time()
    sup._health(p, now)
    sup._health(p, now + 61)
    assert got == [("down", "Test: 송출 중단")]
    p.st.update(state="running", progress_at=now + 100, ffmpeg_started_at=now + 60)
    sup._health(p, now + 100)
    assert got[-1] == ("recovered", "Test: 송출 복구")
