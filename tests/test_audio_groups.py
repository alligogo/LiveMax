"""음악 프로그램 채널별 묶음 (B안): 한 Liquidsoap 이 채널의 라이브들 음악을 각자 포트로 내보낸다."""

import shutil
import socket
import subprocess
import time

import pytest

from conftest import make_live
from livemax import config, liquidsoap, supervisor

HAS = bool(shutil.which("liquidsoap") and shutil.which("ffmpeg"))


def _songs(paths, n=3):
    d = paths.audio / "a"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        f = d / f"s{i}.m4a"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=f={300 + 50 * i}:d=15",
                        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", str(f)], check=True)


def _open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_render_group_keeps_each_live_separate(paths, settings):
    lives = [make_live(id=f"live{i}", channel_id="ch1", audio_folders=["a"], harbor_port=8400 + i, restart_slot=i) for i in range(3)]
    txt = liquidsoap.render_group(lives, paths, settings, "ch-ch1")
    assert txt.count("def setup_live") == 3 and txt.count("output.harbor(") == 3
    assert all(f"port={8400 + i}" in txt for i in range(3))
    assert "audio-g-ch-ch1.log" in txt and txt.count("settings.log.file.path.set(") == 1
    assert liquidsoap.group_key(lives[0]) == "ch-ch1" and liquidsoap.group_key(make_live(id="x9")) == "solo-x9"


@pytest.mark.skipif(not HAS, reason="liquidsoap/ffmpeg 미설치")
def test_group_check_passes(paths, settings, tmp_path):
    _songs(paths)
    lives = [make_live(id=f"live{i}", channel_id="ch1", audio_folders=["a"], harbor_port=8400 + i, restart_slot=i,
                       crossfade=(3 if i == 1 else 0)) for i in range(3)]
    p = liquidsoap.write_group(lives, paths, settings, "ch-ch1")
    r = subprocess.run(["liquidsoap", "--no-deprecated", "--check", str(p)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not HAS, reason="liquidsoap/ffmpeg 미설치")
def test_supervisor_runs_one_process_per_channel(paths, settings):
    _songs(paths)
    settings["audio_shared"] = True
    store = config.LiveStore(paths, settings)
    for i in range(3):
        store.save(make_live(id=f"live{i + 1}", channel_id="ch1", key_id=f"k{i + 1}", audio_folders=["a"], harbor_port=8410 + i, restart_slot=i))
    sup = supervisor.Supervisor(paths, settings)
    assert sup.shared_audio and sup.audio_log_name("live2") == "audio-g-ch-ch1"
    try:
        sup.groups_tick()
        assert "ch-ch1" in sup.groups and sup.groups["ch-ch1"]["proc"] is None  # 송출 중인 라이브가 없으면 안 띄움
        sup.proc("live1").desired = True  # 하나만 켜도 묶음 전체(꺼 둔 라이브 몫까지)가 뜬다
        sup.groups_tick()
        pr = sup.groups["ch-ch1"]["proc"]
        assert pr is not None and pr.poll() is None
        for _ in range(80):
            if all(_open(8410 + i) for i in range(3)):
                break
            time.sleep(0.25)
        assert all(_open(8410 + i) for i in range(3))
        assert sup.group_pid("live3") == pr.pid  # 세 라이브가 같은 프로그램
        sup.groups_tick()
        assert sup.groups["ch-ch1"]["proc"] is pr  # 바뀐 게 없으면 그대로
        lv = store.get("live2")
        lv.audio_mode = "random"
        store.save(lv)  # 재생 방식 변경 → 그 채널 묶음만 다시
        sup.groups_tick()
        assert sup.groups["ch-ch1"]["proc"] is not pr and pr.poll() is not None
        sup.proc("live1").desired = False  # 송출 중인 라이브가 없어지면 끈다
        sup.groups_tick()
        assert sup.groups["ch-ch1"]["proc"] is None
    finally:
        for g in sup.groups.values():
            supervisor.LiveProc._kill(g.get("proc"))


def test_group_keys_cap_by_live_number():
    """채널 하나에 라이브가 많아도 묶음당 최대 N개 — 라이브 번호 순으로 나눈다. 첫 묶음 이름은 예전 그대로."""
    lives = [make_live(id=f"live{i}", channel_id="ch1") for i in (3, 1, 12, 2, 25, 11)] + [make_live(id="live9", channel_id="ch2")]
    g = liquidsoap.group_keys(lives, 2)
    assert g["live1"] == g["live2"] == "ch-ch1" and g["live3"] == g["live11"] == "ch-ch1-2"
    assert g["live12"] == g["live25"] == "ch-ch1-3" and g["live9"] == "ch-ch2"
    g10 = liquidsoap.group_keys(lives, 10)
    assert set(g10.values()) == {"ch-ch1", "ch-ch2"}  # 10개 이하 채널은 지금과 같음
    # 새 라이브(큰 번호)가 늘어도 앞 묶음 구성은 그대로
    g2 = liquidsoap.group_keys(lives + [make_live(id="live40", channel_id="ch1")], 2)
    assert all(g2[k] == g[k] for k in g)
    assert liquidsoap.group_keys([make_live(id="live5", channel_id="")], 10)["live5"] == "solo-live5"


def test_group_log_view_filters_and_names(client, paths, settings):
    """음악 묶음 로그: 보는 라이브 줄 + 공통 줄만, 내부 이름(live13_pl)은 사람이 읽는 이름으로."""
    from test_api import NEW, ORIGIN, login
    login(client, client.initial_pw)
    client.post("/api/password", json={"current": client.initial_pw, "new": NEW}, headers=ORIGIN)
    sup = client.core.supervisor
    sup.shared_audio = True
    for i in (1, 13):
        sup.store.save(make_live(id=f"live{i}", name=f"LIVE {i}", channel_id="ch1", key_id=f"k{i}", audio_folders=["a"],
                                 harbor_port=8500 + i, restart_slot=i))
    paths.logs.mkdir(parents=True, exist_ok=True)
    (paths.logs / "audio-g-ch-ch1.log").write_text(
        "2026/11/09 01:00:00 [main:3] Liquidsoap 2.2\n"
        "2026/11/09 01:00:01 [live1_pl:3] Prepared song-a\n"
        "2026/11/09 01:00:02 [live13_pl:3] Prepared song-b\n"
        "2026/11/09 01:00:03 [clock.live13_music:3] catchup\n", encoding="utf-8")
    t = client.get("/api/logs", params={"name": "audio-live13"}).json()["text"]
    assert "song-b" in t and "song-a" not in t and "Liquidsoap 2.2" in t
    assert "LIVE 13 · live13 · 재생목록" in t and "음악 시계" in t and "live13_pl" not in t
    assert "함께 도는 라이브 1개: LIVE 1" in t
    t1 = client.get("/api/logs", params={"name": "audio-live1"}).json()["text"]
    assert "song-a" in t1 and "song-b" not in t1 and "catchup" not in t1
