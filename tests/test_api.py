"""웹 API: 로그인·비밀번호 강제 변경·차단, 라이브 생성(키 마스킹), 미디어 폴더, 이어 올리기, 재부팅 표시."""

import time
import warnings

warnings.filterwarnings("ignore")
ORIGIN = {"origin": "https://testserver"}
NEW = "Correct-Horse-Battery-9"


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
    # 채널 + 스트림 키 (키는 채널에서만 등록)
    assert client.post("/api/channels", json={"name": "재즈 채널"}, headers=ORIGIN).json()["id"] == "ch1"
    assert client.post("/api/channels/ch1/keys", json={"label": "메인", "key": "fake-0000-0000-0000-9z9z"},
                       headers=ORIGIN).json()["id"] == "k1"


def test_create_live_masks_key(client):
    _ready(client)
    r = client.post("/api/lives", headers=ORIGIN, json={"name": "재즈", "channel_id": "ch1", "key_id": "k1",
                                                       "video_folders": ["Sax After Six"], "audio_folders": ["재즈-색소폰"], "apply": "later"})
    assert r.json() == {"ok": True, "id": "live1"}
    ov = client.get("/api/overview").text
    assert "fake-0000" not in ov and "****9z9z" in ov
    assert client.get("/api/media/video").json()["folders"][0]["used_by"] == ["live1"]
    # 사용 중인 폴더는 이름 변경·삭제 불가
    assert client.delete("/api/media/video/Sax After Six", headers=ORIGIN).status_code == 400


def test_create_live_requires_folders_and_key(client):
    _ready(client)
    r = client.post("/api/lives", headers=ORIGIN, json={"name": "x", "channel_id": "ch1", "key_id": "k1", "video_folders": [], "audio_folders": ["재즈-색소폰"]})
    assert r.status_code == 400 and "비디오 폴더" in r.json()["error"]
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


def test_delete_blocked_in_used_folder(client):
    _ready(client)
    client.post("/api/lives", headers=ORIGIN, json={"name": "재즈", "channel_id": "ch1", "key_id": "k1",
                                                  "video_folders": ["Sax After Six"], "audio_folders": ["재즈-색소폰"], "apply": "later"})
    p = client.core.paths.video / "Sax After Six" / "a.mp4"
    p.write_bytes(b"x")
    r = client.delete("/api/media/video/Sax After Six/a.mp4", headers=ORIGIN)
    assert r.status_code == 400 and "재즈" in r.json()["error"] and p.exists()  # 비디오: 예약 없이 그냥 거부
    client.put("/api/lives/live1", headers=ORIGIN, json={"audio_folders": ["재즈-색소폰"], "video_folders": ["Sax After Six"]})
    client.post("/api/media/audio", json={"name": "free"}, headers=ORIGIN)
    q = client.core.paths.audio / "free" / "b.m4a"
    q.write_bytes(b"x")
    assert client.delete("/api/media/audio/free/b.m4a", headers=ORIGIN).json()["ok"] and not q.exists()  # 바로 삭제


def test_timer_fields_roundtrip(client):
    _ready(client)
    client.post("/api/lives", headers=ORIGIN, json={"name": "재즈", "channel_id": "ch1", "key_id": "k1",
                                                  "video_folders": ["Sax After Six"], "audio_folders": ["재즈-색소폰"], "apply": "later",
                                                  "timer_mode": "duration", "timer_minutes": 710, "timer_repeat": True,
                                                  "timer_gap_minutes": 3})
    lv = client.get("/api/overview").json()["lives"][0]
    assert (lv["timer_mode"], lv["timer_minutes"], lv["timer_repeat"], lv["timer_gap_minutes"]) == ("duration", 710, True, 3)
    assert lv["timer_end_at"] is None and lv["next_start_at"] is None and "bitrate" not in lv
    r = client.put("/api/lives/live1", headers=ORIGIN, json={"timer_mode": "until", "timer_until": 0})
    assert r.status_code == 400
    # 지난 종료 시각으로는 시작할 수 없다
    client.put("/api/lives/live1", headers=ORIGIN, json={"timer_mode": "until", "timer_until": time.time() - 10})
    assert client.post("/api/lives/live1/start", headers=ORIGIN).status_code == 400


def test_reboot_flag_shown(client):
    _ready(client)
    assert client.get("/api/overview").json()["reboot_required"] is False
    flag = client.core.paths.state / "host" / "reboot-required"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("*** System restart required ***")
    import time as _t; _t.sleep(4.1)  # 개요는 4초 동안 캐시됨 (여러 기기가 열어도 한 번만 계산)
    ov = client.get("/api/overview").json()
    assert ov["reboot_required"] is True and any("재부팅" in a["message"] for a in ov["alerts"])


def test_spa_served(client):
    assert "LiveMax" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200


def test_update_result_shown(client):
    _ready(client)
    log = client.core.paths.logs / "update.log"
    log.write_text("2026-11-03T16:12:16Z 웹 요청으로 업데이트\nStatus: Image is up to date for x\n")
    assert client.get("/api/system").json()["update_last"] == {"at": "2026-11-03T16:12:16Z", "result": "latest"}
    with open(log, "a") as f:
        f.write("2026-11-03T16:19:43Z 웹 요청으로 업데이트\nStatus: Downloaded newer image for x\n")
    assert client.get("/api/system").json()["update_last"]["result"] == "updated"


def test_stopped_live_shows_no_stale_stats(client):
    _ready(client)
    client.post("/api/lives", headers=ORIGIN, json={"name": "재즈", "channel_id": "ch1", "key_id": "k1",
                                                  "video_folders": ["Sax After Six"], "audio_folders": ["재즈-색소폰"], "apply": "later"})
    p = client.core.supervisor.proc("live1")
    p.set(state="running", bitrate=5186.0, clip="1.mp4", clip_folder="Sax After Six", out_time=100.0)
    p.stop("사용자 정지")
    lv = client.get("/api/overview").json()["lives"][0]
    assert lv["bitrate_kbps"] is None and lv["clip"] is None and lv["out_time"] is None
