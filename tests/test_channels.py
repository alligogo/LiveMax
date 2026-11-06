"""v2.4 채널: 키는 채널에서만 등록·마스킹, 한 키는 한 라이브만, 로고 검증, 기존 키 자동 이전, 폴더 통째 삭제."""

import pytest

from conftest import make_live
from test_api import ORIGIN, _ready
from livemax import channels, config, secrets

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
LIVE = {"video_folders": ["Sax After Six"], "audio_folders": ["재즈-색소폰"], "apply": "later"}


def test_keys_masked_and_single_use(client):
    _ready(client)
    c = client
    assert c.post("/api/channels/ch1/keys", json={"label": "중복", "key": "fake-0000-0000-0000-9z9z"}, headers=ORIGIN).status_code == 400
    assert c.post("/api/channels/ch1/keys", json={"label": "서브", "key": "fake-1111-1111-1111-7y7y"}, headers=ORIGIN).json()["id"] == "k2"
    assert c.post("/api/lives", headers=ORIGIN, json={"name": "A", "channel_id": "ch1", "key_id": "k1", **LIVE}).json()["ok"]
    r = c.post("/api/lives", headers=ORIGIN, json={"name": "B", "channel_id": "ch1", "key_id": "k1", **LIVE})
    assert r.status_code == 400 and "'A'" in r.json()["error"]  # 같은 키는 다른 라이브에서 못 씀
    body = c.get("/api/channels").text
    assert "fake-0000" not in body and "fake-1111" not in body and "****9z9z" in body
    keys = c.get("/api/channels").json()["channels"][0]["keys"]
    assert [(k["id"], k["status"], k["used_by_name"]) for k in keys] == [("k1", "registered", "A"), ("k2", "free", None)]
    assert c.delete("/api/channels/ch1/keys/k1", headers=ORIGIN).status_code == 400  # 쓰는 키는 삭제 불가
    assert c.delete("/api/channels/ch1", headers=ORIGIN).status_code == 400          # 라이브가 있는 채널은 삭제 불가
    assert c.delete("/api/channels/ch1/keys/k2", headers=ORIGIN).json()["ok"]
    assert not channels.ChannelStore(c.core.paths).key_path("ch1", "k2").exists()
    # 다른 채널의 키 ID 를 섞어 쓸 수 없음
    c.post("/api/channels", json={"name": "둘째"}, headers=ORIGIN)
    assert c.post("/api/lives", headers=ORIGIN, json={"name": "C", "channel_id": "ch2", "key_id": "k1", **LIVE}).status_code == 400


def test_channel_fields_and_logo(client):
    _ready(client)
    c = client
    assert c.put("/api/channels/ch1", json={"url": "javascript:alert(1)"}, headers=ORIGIN).status_code == 400
    assert c.put("/api/channels/ch1", json={"url": "https://www.youtube.com/@jazz", "description": "재즈"}, headers=ORIGIN).json()["ok"]
    assert c.post("/api/channels/ch1/logo", content=b"<svg onload=alert(1)>", headers=ORIGIN).status_code == 400
    assert c.post("/api/channels/ch1/logo", content=PNG, headers=ORIGIN).json()["ok"]
    ch = c.get("/api/overview").json()["channels"][0]
    assert ch["url"] == "https://www.youtube.com/@jazz" and ch["logo"].startswith("/api/channels/ch1/logo?v=")
    r = c.get(ch["logo"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG


def test_migrate_legacy_keys(paths, settings):
    ls = config.LiveStore(paths, settings)
    for i, key in ((1, "fake-aaaa-aaaa-aaaa-0001"), (2, "fake-bbbb-bbbb-bbbb-0002")):
        ls.save(make_live(id=f"live{i}", name=f"라이브{i}", harbor_port=8100 + i, restart_slot=i))
        secrets.write_secret(paths.stream_key(f"live{i}"), key)
    assert "2개" in channels.migrate_legacy(paths, settings)
    cs = channels.ChannelStore(paths)
    ch = cs.all()[0]
    assert ch.name == "내 채널" and [k["label"] for k in ch.keys] == ["라이브1", "라이브2"]
    lv2 = ls.get("live2")
    assert (lv2.channel_id, lv2.key_id) == ("ch1", "k2") and not paths.stream_key("live2").exists()
    assert secrets.read_secret(channels.live_key_path(paths, lv2)) == "fake-bbbb-bbbb-bbbb-0002"
    assert channels.migrate_legacy(paths, settings) is None  # 1번만


def test_folder_delete_removes_everything(client):
    _ready(client)
    p = client.core.paths
    (p.audio / "재즈-색소폰" / "a.m4a").write_bytes(b"x" * 10)
    (p.incoming / "audio" / "재즈-색소폰").mkdir(parents=True, exist_ok=True)
    (p.incoming / "audio" / "재즈-색소폰" / "b.m4a").write_bytes(b"x")
    assert client.delete("/api/media/audio/재즈-색소폰", headers=ORIGIN).status_code == 400            # 확인 이름 없음
    assert client.delete("/api/media/audio/재즈-색소폰?confirm=틀림", headers=ORIGIN).status_code == 400
    r = client.delete("/api/media/audio/재즈-색소폰?confirm=재즈-색소폰", headers=ORIGIN)
    assert r.json()["ok"] and "1개" in r.json()["message"]
    assert not (p.audio / "재즈-색소폰").exists() and not (p.incoming / "audio" / "재즈-색소폰").exists()


def test_folder_in_use_cannot_be_deleted(client):
    _ready(client)
    client.post("/api/lives", headers=ORIGIN, json={"name": "A", "channel_id": "ch1", "key_id": "k1", **LIVE})
    r = client.delete("/api/media/video/Sax After Six?confirm=Sax After Six", headers=ORIGIN)
    assert r.status_code == 400 and "'A'" in r.json()["error"]


@pytest.mark.parametrize("kbps,ok", [(128, True), (160, True), (192, True), (256, False)])
def test_audio_bitrate_range(settings, kbps, ok):
    lo, hi = config.parse_bitrate(settings["spec"]["audio"]["bitrate_min"]), config.parse_bitrate(settings["spec"]["audio"]["bitrate_max"])
    tol = settings["spec"]["audio"]["bitrate_tolerance"]
    assert (lo * (1 - tol) <= kbps <= hi * (1 + tol)) is ok


def test_song_delete_in_live_folder(client):
    """송출 중인 음악 폴더: 지금 곡·다음 곡은 거부, 나머지는 목록에서 먼저 뺀 뒤 삭제."""
    import json

    from livemax import liquidsoap

    _ready(client)
    p = client.core.paths
    folder = p.audio / "재즈-색소폰"
    for n in ("now.m4a", "next.m4a", "free.m4a"):
        (folder / n).write_bytes(b"x")
    client.post("/api/lives", headers=ORIGIN, json={"name": "A", "channel_id": "ch1", "key_id": "k1", **LIVE})
    lv = client.core.supervisor.store.get("live1")
    client.core.supervisor.proc("live1").desired = True  # 송출 중으로 간주 (실제 프로세스는 띄우지 않음)
    (p.run_dir / "live1.nowplaying.json").write_text(json.dumps({"file": str(folder / "now.m4a")}))
    liquidsoap.next_path(lv, p).write_text(json.dumps({"file": str(folder / "next.m4a")}))
    liquidsoap.write_m3u(lv, p)

    files = {f["name"]: f for f in client.get("/api/media/audio/재즈-색소폰").json()["files"]}
    assert files["now.m4a"]["playing"] == [{"live": "A", "channel": "재즈 채널"}] and files["next.m4a"]["next"][0]["live"] == "A" and not files["free.m4a"]["playing"]
    r = client.delete("/api/media/audio/재즈-색소폰/now.m4a", headers=ORIGIN)
    assert r.status_code == 400 and "재생 중" in r.json()["error"] and (folder / "now.m4a").exists()
    r = client.delete("/api/media/audio/재즈-색소폰/next.m4a", headers=ORIGIN)
    assert r.status_code == 400 and "다음" in r.json()["error"] and (folder / "next.m4a").exists()
    assert client.delete("/api/media/audio/재즈-색소폰/free.m4a", headers=ORIGIN).json()["ok"]
    m3u = liquidsoap.m3u_path(lv, p).read_text()
    assert not (folder / "free.m4a").exists() and "free.m4a" not in m3u and "now.m4a" in m3u
    assert not liquidsoap.DELETING
    # 비디오는 송출 중인 폴더면 여전히 삭제 불가
    (p.video / "Sax After Six" / "c.mp4").write_bytes(b"x")
    assert client.delete("/api/media/video/Sax After Six/c.mp4", headers=ORIGIN).status_code == 400


def test_new_live_waits_until_started(client):
    """새 라이브는 저장만 — '대기중'(stopped) + 꺼짐. 부팅 때 저절로 시작되지 않는다."""
    _ready(client)
    client.post("/api/lives", headers=ORIGIN, json={"name": "새것", "channel_id": "ch1", "key_id": "k1", **LIVE})
    lv = client.get("/api/overview").json()["lives"][0]
    assert lv["enabled"] is False and lv["desired"] is False and lv["status"] == "stopped"
    assert client.get("/api/overview").json()["capacity"]["lives"] == 0
    assert not [a for a in client.get("/api/overview").json()["alerts"] if "중단" in a["message"]]


def test_never_started_enabled_live_fixed_on_boot(paths, settings, monkeypatch):
    from conftest import make_live
    from livemax import daemon

    store = config.LiveStore(paths, settings)
    store.save(make_live(id="live1", enabled=True))                       # 시작한 적 없음 (세션 없음)
    store.save(make_live(id="live2", enabled=True, harbor_port=8102, restart_slot=1))
    (paths.run_dir / "live2.session.json").write_text('{"started_at": 1}')  # 송출하던 라이브
    monkeypatch.setenv("LIVEMAX_DATA", str(paths.root))
    daemon.App()
    assert store.get("live1").enabled is False and store.get("live2").enabled is True


def test_max_lives_limit(client):
    """안전장치: 라이브 총 개수가 설정한 최대 개수(기본 15)에 닿으면 더 못 만든다."""
    _ready(client)
    assert client.get("/api/system").json()["settings"]["max_lives"] == 40  # 기본값 (OVH VPS-2 실측 기준, DESIGN 11-D)
    assert client.put("/api/system/settings", headers=ORIGIN, json={"max_lives": 0}).status_code == 400
    assert client.put("/api/system/settings", headers=ORIGIN, json={"max_lives": 2}).status_code == 200
    for i in (2, 3):
        client.post("/api/channels/ch1/keys", headers=ORIGIN, json={"label": f"키{i}", "key": f"fake-000{i}-0000-0000-abc{i}"})
    kids = [k["id"] for k in client.get("/api/channels").json()["channels"][0]["keys"]]
    for i in range(2):
        r = client.post("/api/lives", headers=ORIGIN, json={"name": f"L{i}", "channel_id": "ch1", "key_id": kids[i], **LIVE})
        assert r.status_code == 200, r.text
    r = client.post("/api/lives", headers=ORIGIN, json={"name": "넘침", "channel_id": "ch1", "key_id": kids[2], **LIVE})
    assert r.status_code == 400 and "최대 개수" in r.json()["error"]
    assert len(client.get("/api/overview").json()["lives"]) == 2
    cap = client.get("/api/overview").json()["capacity"]
    assert cap["can_add"] == 0 and cap["limiting"] == "max"
