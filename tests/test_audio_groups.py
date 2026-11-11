"""음악 프로그램 채널별 묶음 (B안): 한 Liquidsoap 이 채널의 라이브들 음악을 각자 포트로 내보낸다."""

import shutil
import socket
import subprocess
import time

import pytest

from conftest import make_live
from livemax import config, ffmpeg_cmd, liquidsoap, supervisor

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


class _Sink:
    """ffmpeg 가 보내는 FLV 를 받아 영상·소리 조각을 센다 (유튜브 자리)."""

    def __init__(self):
        import threading

        from livemax.rtmp_relay import FlvParser

        self.v = self.a = 0
        self.last_a = time.time()
        self.max_a_gap = 0.0
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self._parser = FlvParser
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        c, _ = self.srv.accept()
        ps, buf, hdr = self._parser(), b"", False
        while True:
            b = c.recv(65536)
            if not b:
                return
            if not hdr:
                buf += b
                if len(buf) < 13:
                    continue
                b, hdr = buf[13:], True
            for t, _ts, _d in ps.feed(b):
                if t == 9:
                    self.v += 1
                elif t == 8:
                    now = time.time()
                    if self.a:
                        self.max_a_gap = max(self.max_a_gap, now - self.last_a)
                    self.a, self.last_a = self.a + 1, now

    def audio_flowing(self, sec=3.0):
        a0 = self.a
        time.sleep(sec)
        return self.a - a0 > sec * 20  # AAC 44.1kHz ≈ 초당 43조각


def _video(paths):
    v = paths.root / "v.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=6", "-c:v", "libx264",
                    "-g", "30", "-b:v", "300k", "-pix_fmt", "yuv420p", str(v)], check=True)
    lst = paths.root / "list.ffconcat"
    lst.write_text("ffconcat version 1.0\n" + "".join(f"file '{v}'\n" for _ in range(100)))
    return lst


def _wait(fn, timeout=25.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        r = fn()
        if r:
            return r
        time.sleep(step)
    return None


def _sup(paths, settings, n=2):
    settings["audio_shared"] = True
    store = config.LiveStore(paths, settings)
    for i in range(1, n + 1):
        store.save(make_live(id=f"live{i}", channel_id="ch1", key_id=f"k{i}", audio_folders=["a"], harbor_port=8410 + i, restart_slot=i))
    return supervisor.Supervisor(paths, settings), store


def _stop(sup, *procs):
    sup.stop_event.set()
    for p in procs:
        if p and p.poll() is None:
            p.kill()
    for g in sup.groups.values():
        supervisor.LiveProc._kill(g.get("proc"))


def test_dynamic_script_has_no_members_and_entry_fields(paths, settings):
    """묶음 스크립트에는 라이브가 들어 있지 않다 (목록 파일로 넣고 뺌) → 라이브가 늘어도 스크립트는 그대로."""
    txt = liquidsoap.render_dynamic(paths, settings, "ch-ch1")
    assert "members.json" in txt and "audio-g-ch-ch1.log" in txt and "live1" not in txt
    lv = make_live(id="live7", channel_id="ch1", audio_folders=["a"], crossfade=3)
    e = liquidsoap.member_entry(lv, paths, "live7_4", 9001)
    assert e["id"] == "live7_4" and e["mount"] == "live7_4.aac" and e["port"] == 9001 and e["cf"] == 3.0 and e["live"] == "live7"
    assert liquidsoap.audio_sig(lv) != liquidsoap.audio_sig(make_live(id="live7", channel_id="ch1", audio_folders=["a"]))
    assert liquidsoap.write_members(paths, "ch-ch1", [e]) is True and liquidsoap.write_members(paths, "ch-ch1", [e]) is False


@pytest.mark.skipif(not HAS, reason="liquidsoap/ffmpeg 미설치")
def test_dynamic_script_check_passes(paths, settings):
    for buf in (2.0, 0):
        settings["audio_buffer_seconds"] = buf
        p = liquidsoap.write_dynamic(paths, settings, "ch-ch1")
        r = subprocess.run(["liquidsoap", "--no-deprecated", "--check", str(p)], capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr


def test_assignment_is_sticky(paths, settings):
    """한 번 잡은 묶음 자리는 다른 라이브가 생기거나 지워져도 안 바뀐다 (옮기면 그 라이브 음악이 끊김). 새 라이브만 빈자리로."""
    settings["audio_group_max"] = 2
    sup, store = _sup(paths, settings, 3)
    lives = store.all()
    m = dict(sup._assign(lives))
    assert m == {"live1": "ch-ch1", "live2": "ch-ch1", "live3": "ch-ch1-2"}
    store.delete("live1")  # 앞 묶음에 빈자리 — live3 은 그대로, 새 라이브가 그 자리로
    store.save(make_live(id="live9", channel_id="ch1", key_id="k9", audio_folders=["a"], harbor_port=8499, restart_slot=9))
    m2 = dict(sup._assign(store.all()))
    assert m2["live2"] == "ch-ch1" and m2["live3"] == "ch-ch1-2" and m2["live9"] == "ch-ch1" and "live1" not in m2
    lv = store.get("live2")
    lv.channel_id, lv.key_id = "ch2", "k2"
    store.save(lv)  # 채널을 옮기면 그 라이브만 새 자리
    assert dict(sup._assign(store.all()))["live2"] == "ch-ch2"


@pytest.mark.skipif(not HAS, reason="liquidsoap/ffmpeg 미설치")
def test_add_remove_change_without_restarting_group(paths, settings):
    """v2.12 사고 재현 방지: 송출 중인 묶음에 라이브를 추가·삭제·설정 변경해도 묶음 프로그램은 그대로, 기존 라이브 소리는 안 끊긴다."""
    _songs(paths)
    sup, store = _sup(paths, settings, 2)
    ff = None
    try:
        sup.groups_tick()
        assert sup.groups["ch-ch1"]["proc"] is None  # 송출 중인 라이브가 없으면 안 띄움
        sup.proc("live1").desired = True
        sup.groups_tick()
        g = sup.groups["ch-ch1"]
        pr = g["proc"]
        assert pr is not None and pr.poll() is None and set(g["inst"]) == {"live1"}  # 켠 라이브만 올림
        url1 = _wait(lambda: sup.audio_url("live1"))
        assert url1 and url1.endswith("/live1_1.aac") and sup.audio_url("live2") is None
        sink = _Sink()
        ff = subprocess.Popen(ffmpeg_cmd.build(_video(paths), url1, f"tcp://127.0.0.1:{sink.port}", extra_out=["-flush_packets", "0"]),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        assert _wait(lambda: sink.a > 50)
        sink.max_a_gap = 0.0
        # 1) 같은 채널에 새 라이브 추가 (41번째 라이브 상황) + 기존 live2 시작
        store.save(make_live(id="live3", channel_id="ch1", key_id="k3", audio_folders=["a"], harbor_port=8413, restart_slot=3))
        sup.proc("live3").desired = True
        sup.proc("live2").desired = True
        sup.groups_tick()
        assert g["proc"] is pr and pr.poll() is None  # 다시 켜지 않음
        assert _wait(lambda: sup.audio_url("live3")) and _wait(lambda: sup.audio_url("live2"))
        # 2) live2 재생 방식 변경 → live2 만 새 주소, live1 주소는 그대로
        old2 = sup.audio_url("live2")
        lv = store.get("live2")
        lv.audio_mode = "random"
        store.save(lv)
        assert sup.audio_url("live2") is None  # 옛 설정의 주소는 더 내주지 않는다 (ffmpeg 가 옛 주소로 붙지 않게)
        sup.groups_tick()
        new2 = _wait(lambda: sup.audio_url("live2"))
        assert new2 and new2 != old2 and sup.audio_url("live1") == url1
        assert not sup._mount_ok(g["port"], old2.rsplit("/", 1)[1])  # 옛 주소는 내려감
        # 3) live3 삭제 → live3 만 내려감
        tok3 = g["inst"]["live3"]["token"]
        sup.procs.pop("live3").desired = False
        store.delete("live3")
        sup.groups_tick()
        assert _wait(lambda: not sup._mount_ok(g["port"], tok3 + ".aac"))
        # 4) 같은 id 의 새 라이브가 다시 생겨도 (id·포트 재사용) 새 이름으로 올라간다
        store.save(make_live(id="live3", channel_id="ch1", key_id="k3", audio_folders=["a"], harbor_port=8413, restart_slot=3))
        sup.proc("live3").desired = True
        sup.groups_tick()
        again = _wait(lambda: sup.audio_url("live3"))
        assert again and not again.endswith(tok3 + ".aac")
        # 그동안 live1 의 소리는 한 번도 끊기지 않았다
        assert g["proc"] is pr and pr.poll() is None and ff.poll() is None
        assert sink.audio_flowing() and sink.max_a_gap < 1.5, sink.max_a_gap
        # 5) 송출 중인 라이브가 없어지면 프로그램도 끈다
        for i in ("live1", "live2", "live3"):
            sup.proc(i).desired = False
        sup.groups_tick()
        assert g["proc"] is None and pr.poll() is not None
    finally:
        _stop(sup, ff)


@pytest.mark.skipif(not HAS, reason="liquidsoap/ffmpeg 미설치")
def test_ffmpeg_reattaches_audio_after_group_crash(paths, settings):
    """음악 묶음 프로그램이 죽어도: 바로 다시 뜨고(같은 포트·같은 주소), ffmpeg 가 스스로 다시 붙어 소리가 돌아온다.
    (v2.12 까지: ffmpeg 는 죽지 않고 소리 없는 영상만 계속 보냄 — 감시에는 정상으로 보임)"""
    _songs(paths)
    sup, _store = _sup(paths, settings, 1)
    ff = None
    try:
        sup.proc("live1").desired = True
        sup.groups_tick()
        g = sup.groups["ch-ch1"]
        url = _wait(lambda: sup.audio_url("live1"))
        assert url
        sink = _Sink()
        ff = subprocess.Popen(ffmpeg_cmd.build(_video(paths), url, f"tcp://127.0.0.1:{sink.port}", extra_out=["-flush_packets", "0"]),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        assert _wait(lambda: sink.a > 50)
        glog = paths.logs / "audio-g-ch-ch1.log"
        assert _wait(lambda: glog.exists() and "▶" in glog.read_text(errors="replace"))  # v2.17: 곡 시작 줄이 남는다
        old = g["proc"]
        old.kill()  # 사고: 음악 프로그램이 갑자기 죽음
        assert _wait(lambda: g["proc"] is not None and g["proc"] is not old and g["proc"].poll() is None, timeout=15)  # 5초 주기를 안 기다림
        assert sup.audio_url("live1") in (None, url)
        assert _wait(lambda: sup.audio_url("live1") == url, timeout=20)  # 같은 주소로 다시 올라옴
        a0, v0 = sink.a, sink.v
        assert _wait(lambda: sink.a > a0 + 100 and sink.v > v0 + 60, timeout=40), (sink.a - a0, sink.v - v0)  # 소리·영상 모두 다시 흐름
        assert ff.poll() is None
    finally:
        _stop(sup, ff)


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
        "2026/11/09 01:00:03 [clock.live13_music:3] catchup\n"
        "2026/11/09 01:00:04 [group:2] ADD live13_2\n"
        "2026/11/09 01:00:05 [live13_2_out:3] new-format-line\n"
        "2026/11/09 01:00:06 [live1_7_pl:3] other-new\n"
        "2026/11/09 01:00:07 [decoder:2] Decoding \"/data/media/audio/a/x.m4a\" ended: Ffmpeg_decoder.End_of_file.\n"
        "2026/11/09 01:00:08 [live13_2_track:2] ▶ Last Summer Scent\n"
        "2026/11/09 01:00:09 [live1_7_track:2] ▶ Other Song\n", encoding="utf-8")
    t = client.get("/api/logs", params={"name": "audio-live13"}).json()["text"]
    assert "song-b" in t and "song-a" not in t and "Liquidsoap 2.2" in t
    assert "LIVE 13 · live13 · 재생목록" in t and "음악 시계" in t and "live13_pl" not in t
    assert "음악 올림: LIVE 13 · live13" in t and "LIVE 13 · live13 · 출력:3] new-format-line" in t and "other-new" not in t
    assert "함께 도는 라이브 1개: LIVE 1" in t
    # v2.17: 곡 끝 줄은 숨기고(개수만), 곡 시작은 '▶ 제목' 만, 다른 라이브 곡은 안 보임
    assert "End_of_file" not in t and "곡 끝 기록 1줄 숨김" in t
    assert "01:00:08 ▶ Last Summer Scent" in t and "Other Song" not in t
    t1 = client.get("/api/logs", params={"name": "audio-live1"}).json()["text"]
    assert "song-a" in t1 and "song-b" not in t1 and "catchup" not in t1
