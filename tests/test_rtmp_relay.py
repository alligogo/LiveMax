"""새 송출 중계기(lmrelay): RTMP 조각·확장 타임스탬프·AMF·FLV 파싱, 그리고 실제 ffmpeg RTMP 서버로 중계."""

import asyncio
import json
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from livemax import rtmp_relay as rr

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _dechunk(data: bytes, chunk: int):
    """encode_message 결과를 다시 풀어 (헤더 타임스탬프, 확장값, 내용) 확인용."""
    b0 = data[0]
    assert b0 >> 6 == 0
    ts3 = int.from_bytes(data[1:4], "big")
    ln = int.from_bytes(data[4:7], "big")
    pos = 12
    ext = None
    if ts3 == 0xFFFFFF:
        ext = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
    out = data[pos:pos + min(chunk, ln)]
    pos += len(out)
    while len(out) < ln:
        assert data[pos] >> 6 == 3
        pos += 1
        if ext is not None:
            assert struct.unpack(">I", data[pos:pos + 4])[0] == ext  # 이어지는 조각에도 확장값
            pos += 4
        part = data[pos:pos + min(chunk, ln - len(out))]
        out += part
        pos += len(part)
    assert pos == len(data)
    return ts3, ext, out


@pytest.mark.parametrize("ts", [0, 1000, 0xFFFFFE, 0xFFFFFF, 16_800_000, 86_400_000 * 3])
def test_chunks_and_extended_timestamp(ts):
    payload = bytes(range(256)) * 50  # 12,800 바이트 → 4조각
    ts3, ext, out = _dechunk(rr.encode_message(6, 9, ts, 1, payload, 4096), 4096)
    assert out == payload
    if ts >= 0xFFFFFF:
        assert ts3 == 0xFFFFFF and ext == ts
    else:
        assert ts3 == ts and ext is None


def test_amf_roundtrip():
    vals = ["_result", 1.0, None, {"code": "NetStream.Publish.Start", "level": "status", "n": 3.0, "ok": True}]
    data = b"".join(rr.amf_enc(v) for v in vals)
    assert rr.amf_dec_all(data) == vals


def test_flv_parser_handles_split_input():
    def tag(t, ts, body):
        return bytes([t]) + len(body).to_bytes(3, "big") + (ts & 0xFFFFFF).to_bytes(3, "big") + bytes([ts >> 24]) \
            + b"\x00\x00\x00" + body + struct.pack(">I", 11 + len(body))
    stream = tag(18, 0, b"meta") + tag(9, 40, b"V" * 5000) + tag(8, 0x1234567, b"A" * 10)
    p = rr.FlvParser()
    got = []
    for i in range(0, len(stream), 777):  # 아무 데서나 잘려 들어와도
        got += p.feed(stream[i:i + 777])
    assert got == [(18, 0, b"meta"), (9, 40, b"V" * 5000), (8, 0x1234567, b"A" * 10)]


def test_handshake_digest_matches_ffmpeg_scheme():
    c1 = rr.make_c1()
    pos = rr._digest_pos(c1, 8, 728, 12)
    assert rr._hmac(rr._FP_KEY[:30], c1[:pos] + c1[pos + 32:]) == c1[pos:pos + 32]
    s1 = bytes(1536)
    assert rr.make_c2(s1) == s1  # 디지스트 없는 서버 → 단순 handshake (S1 을 그대로)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_relay_end_to_end_with_ffmpeg_rtmp_server(tmp_path: Path):
    """ffmpeg → (FLV/TCP) → 중계기 → (RTMP) → ffmpeg RTMP 서버. 받은 쪽에 영상·소리가 이어져 있어야 한다."""
    clip = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-f", "lavfi",
                    "-i", "sine=f=440:sample_rate=48000", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "60", "-bf", "0", "-c:a", "aac", "-shortest", str(clip)], check=True)
    sink_port, relay_port = _free_port(), _free_port()
    out = tmp_path / "out.flv"
    sink = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-listen", "1", "-i",
                             f"rtmp://127.0.0.1:{sink_port}/live2/testkey", "-copyts", "-c", "copy", "-f", "flv", str(out)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log = tmp_path / "relay.log"
    relay = subprocess.Popen([sys.executable, "-m", "livemax.rtmp_relay"], stdin=subprocess.PIPE,
                             env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    try:
        time.sleep(1)
        cfg = {"listen_port": relay_port, "host": "127.0.0.1", "port": sink_port, "app": "live2", "key": "testkey",
               "tcurl": f"rtmp://127.0.0.1:{sink_port}/live2", "tls": False, "log": str(log)}
        relay.stdin.write((json.dumps(cfg) + "\n").encode())
        relay.stdin.close()
        time.sleep(1.5)
        # 4시간 40분(0xFFFFFF ms) 경계를 넘는 타임스탬프로 보낸다 → 확장 타임스탬프 경로 검증
        subprocess.run(["ffmpeg", "-v", "error", "-re", "-i", str(clip), "-c", "copy", "-output_ts_offset", "16774",
                        "-flush_packets", "0", "-f", "flv", f"tcp://127.0.0.1:{relay_port}"], check=True, timeout=60)
        time.sleep(0.5)
        relay.kill()  # 중계기는 영상 교체에 대비해 유튜브 연결을 잠시 살려 두므로, 끝내야 받는 쪽이 마무리한다
        sink.wait(timeout=20)
    finally:
        for p in (relay, sink):
            if p.poll() is None:
                p.kill()
    text = log.read_text(encoding="utf-8")
    assert "송출 시작" in text and "testkey" not in text  # 키는 로그에 안 남는다
    for sel in ("v:0", "a:0"):
        ts = [float(x) for x in subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries", "packet=dts_time", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True).stdout.split()]
        assert len(ts) > 100
        assert ts[0] < 16777 < ts[-1]  # 경계를 넘었고
        assert all(0 <= b - a < 0.2 for a, b in zip(ts, ts[1:]))  # 끊기거나 거꾸로 가지 않음


def test_feed_rewinds_to_latest_keyframe_and_keeps_headers():
    """유튜브 재접속 때: 설정 정보(메타·AVC·AAC 헤더)는 기억하고, 쌓인 것 중 최근 키프레임부터 이어 보낸다."""
    f = rr.Feed()
    meta, avc, aac = (18, 0, b"m"), (9, 0, bytes([0x17, 0]) + b"cfg"), (8, 0, bytes([0xAF, 0]) + b"cfg")
    key1, inter, key2 = (9, 1000, bytes([0x17, 1]) + b"k1"), (9, 1033, bytes([0x27, 1]) + b"p"), (9, 3000, bytes([0x17, 1]) + b"k2")
    a_early, a_late = (8, 2990, bytes([0xAF, 1]) + b"a"), (8, 3010, bytes([0xAF, 1]) + b"b")
    for t, ts, d in (meta, avc, aac, key1, inter, a_early, key2, a_late):
        if t == 18 and f.meta is None:
            f.meta = (t, ts, d)
        elif rr._avc_seq(t, d):
            f.avc = (t, ts, d)
        elif rr._aac_seq(t, d):
            f.aac = (t, ts, d)
        f.q.append((t, ts, d))
    assert f.rewind_to_keyframe() == 3000
    assert list(f.q) == [key2, a_late]  # 키프레임 앞(이른 소리 포함)은 버림
    assert f.headers() == [meta, avc, aac]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_relay_reconnects_without_restarting_ffmpeg(tmp_path: Path):
    """가짜 유튜브가 송출 중간에 끊으면 중계기만 다시 붙고, ffmpeg 는 끝까지 안 멈추며, 새 세션은 키프레임부터."""
    clip = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-f", "lavfi",
                    "-i", "sine=f=440:sample_rate=48000", "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "60", "-bf", "0", "-c:a", "aac", "-shortest", str(clip)], check=True)
    sink_port, relay_port = _free_port(), _free_port()
    outs = [tmp_path / f"out{i}.flv" for i in (1, 2)]

    def sink(out):
        return subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-listen", "1", "-i",
                                 f"rtmp://127.0.0.1:{sink_port}/live2/k", "-c", "copy", "-f", "flv", str(out)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    s1 = sink(outs[0])
    log = tmp_path / "relay.log"
    relay = subprocess.Popen([sys.executable, "-m", "livemax.rtmp_relay"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    s2 = pub = None
    try:
        time.sleep(1)
        relay.stdin.write((json.dumps({"listen_port": relay_port, "host": "127.0.0.1", "port": sink_port, "app": "live2",
                                       "key": "k", "tcurl": f"rtmp://127.0.0.1:{sink_port}/live2", "tls": False,
                                       "log": str(log)}) + "\n").encode())
        relay.stdin.close()
        time.sleep(1.5)
        pub = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-i", str(clip), "-c", "copy", "-flush_packets", "0",
                                "-f", "flv", f"tcp://127.0.0.1:{relay_port}"])
        time.sleep(4)
        s1.kill()            # 유튜브가 끊음
        s1.wait()
        s2 = sink(outs[1])   # 곧 다시 받을 준비
        assert pub.wait(timeout=30) == 0  # ffmpeg 는 재시작 없이 끝까지
        time.sleep(0.5)
        relay.kill()
        s2.wait(timeout=20)
    finally:
        for p in (relay, s1, s2, pub):
            if p and p.poll() is None:
                p.kill()
    text = log.read_text(encoding="utf-8")
    assert "재접속 성공" in text
    flags = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=flags",
                            "-of", "csv=p=0", str(outs[1])], capture_output=True, text=True, check=True).stdout.split()
    assert flags and flags[0].startswith("K")  # 새 세션은 키프레임부터
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(outs[1]), "-f", "null", "-"], capture_output=True, text=True)
    assert dec.stderr.strip() == ""  # 끊김 없이 디코딩됨


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_relay_keeps_youtube_session_when_ffmpeg_is_replaced(tmp_path: Path):
    """영상 교체·일일 재시작: ffmpeg 가 끝나고 새 ffmpeg 가 와도 유튜브 연결은 하나로 이어지고 시각도 이어진다."""
    clip = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-f", "lavfi",
                    "-i", "sine=f=440:sample_rate=48000", "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "60", "-bf", "0", "-c:a", "aac", "-shortest", str(clip)], check=True)
    sink_port, relay_port = _free_port(), _free_port()
    out = tmp_path / "out.flv"
    sink = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-listen", "1", "-i",
                             f"rtmp://127.0.0.1:{sink_port}/live2/k", "-copyts", "-c", "copy", "-f", "flv", str(out)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log = tmp_path / "relay.log"
    relay = subprocess.Popen([sys.executable, "-m", "livemax.rtmp_relay"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    try:
        time.sleep(1)
        relay.stdin.write((json.dumps({"listen_port": relay_port, "host": "127.0.0.1", "port": sink_port, "app": "live2",
                                       "key": "k", "tcurl": f"rtmp://127.0.0.1:{sink_port}/live2", "tls": False,
                                       "log": str(log)}) + "\n").encode())
        relay.stdin.close()
        time.sleep(1.5)
        for _ in range(2):  # 영상 1 → 1초 쉼 → 영상 2
            subprocess.run(["ffmpeg", "-v", "error", "-re", "-i", str(clip), "-c", "copy", "-flush_packets", "0",
                            "-f", "flv", f"tcp://127.0.0.1:{relay_port}"], check=True, timeout=30)
            time.sleep(1)
        relay.kill()          # 그 뒤 연결이 닫혀야 받는 쪽이 파일을 마무리한다
        sink.wait(timeout=20)
    finally:
        for p in (relay, sink):
            if p.poll() is None:
                p.kill()
    text = log.read_text(encoding="utf-8")
    assert text.count("유튜브 송출 시작") == 1 and "유튜브 연결 유지" in text  # 재접속 없이 한 연결
    ts = [float(x) for x in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=dts_time", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True).stdout.split()]
    assert 7.0 < ts[-1] - ts[0] < 11.0       # 4초 + 쉰 시간 1초 남짓 + 4초 — 이어 붙은 하나의 타임라인
    assert all(b >= a for a, b in zip(ts, ts[1:]))  # 거꾸로 가지 않음


def test_relay_reports_slow_youtube_only_when_it_lasts(monkeypatch):
    """유튜브로 못 나가고 밀리면 → 5초 이어질 때 '밀림 시작', 회복 5초 뒤 '밀림 끝' 한 번씩. 평소엔 15초마다 stats 만."""
    events = []
    monkeypatch.setattr(rr, "_emit", events.append)
    r = rr.Relay({})
    r.busy = True
    r.feed = rr.Feed(r.stats)
    w = r._watch_state(0.0)
    bps = 625_000  # 5Mbps
    t = 0.0

    def tick(sent_frac: float):
        nonlocal t
        t += 1
        r.stats["in"] += bps
        r.stats["out"] += int(bps * sent_frac)  # 유튜브 쪽으로 넘긴 양
        r.feed.size += bps - int(bps * sent_frac)  # 못 넘긴 건 중계기 버퍼에 쌓임
        r._watch_step(w, t)

    for _ in range(16):
        tick(1.0)
    assert [e["event"] for e in events] == ["stats"]
    assert 4900 <= events[0]["kbps_in"] <= 5100 and 4900 <= events[0]["kbps_out"] <= 5100
    events.clear()
    for _ in range(4):  # 2초치 쌓일 때까지는 조용
        tick(0.5)
    assert not [e for e in events if e["event"] != "stats"]
    for _ in range(10):
        tick(0.5)
    assert [e["event"] for e in events if e["event"] != "stats"] == ["slow_start"]
    r.feed.size = 0  # 회복: 밀린 게 다 나감
    for _ in range(6):
        tick(1.0)
    ends = [e for e in events if e["event"] == "slow_end"]
    assert len(ends) == 1 and ends[0]["max_delay"] >= 4 and ends[0]["kbps_out"] < ends[0]["kbps_in"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_relay_renews_youtube_session_before_timestamp_wraps(tmp_path: Path, monkeypatch):
    """RTMP 시각은 32비트(약 49.7일)에서 0으로 돌아간다 → 기준을 넘긴 연결은 다음 영상 교체 때 새로 접속, 시각은 0부터."""
    import asyncio

    flv = tmp_path / "a.flv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30", "-t", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-bf", "0", "-f", "flv", str(flv)], check=True)
    data = flv.read_bytes()
    monkeypatch.setattr(rr, "TS_RENEW_MS", 3000)
    monkeypatch.setattr(rr, "BATCH_S", 0.01)

    class FakeW:
        transport = type("T", (), {"get_write_buffer_size": lambda self: 0})()

        def get_extra_info(self, _):
            return None

    class FakePub:
        def __init__(self):
            self.closed, self.w, self.stream_id, self.max_ts = asyncio.Event(), FakeW(), 1, 0

        def tag_bytes(self, t, ts, d):
            self.max_ts = max(self.max_ts, ts)
            return b"x"

        async def send_media(self, _):
            pass

        async def close(self):
            self.closed.set()

    pubs = []

    async def fake_publish(self):
        pubs.append(FakePub())
        return pubs[-1]

    monkeypatch.setattr(rr.Relay, "_publish", fake_publish)

    async def run():
        r = rr.Relay({"listen_port": 0, "host": "x", "port": 1})
        srv = await asyncio.start_server(r.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        for _ in range(3):  # 2초짜리를 세 번 (영상 교체)
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(data)
            await w.drain()
            await asyncio.sleep(0.2)
            w.close()
            await asyncio.sleep(0.2)
        srv.close()
        return r

    r = asyncio.run(run())
    # 1번째: 새 연결(0~2초) → 2번째: 이어 붙임(~4초, 기준 3초 넘김) → 3번째: 새 연결, 시각 다시 0부터
    assert len(pubs) == 2
    assert pubs[0].max_ts > 3000 and pubs[1].max_ts < 2500 and r.last_out < 2500


def test_relay_reports_its_own_stall(monkeypatch):
    """중계기가 몇 초 멈췄다 깨어나면 (서버 CPU 등) 회선 밀림과 따로 'stall' 로 알린다."""
    events = []
    monkeypatch.setattr(rr, "_emit", events.append)
    r = rr.Relay({})
    r.busy = True
    r.feed = rr.Feed(r.stats)
    w = r._watch_state(0.0)
    r._watch_step(w, 1.0)
    r._watch_step(w, 2.1)
    assert not events
    r._watch_step(w, 6.1)  # 4초 만에 깨어남 → 3초 멈춤
    assert events == [{"event": "stall", "sec": 3.0}]


def test_reconnect_gives_up_on_a_hung_server_quickly_and_reresolves(monkeypatch):
    """유튜브가 끊은 뒤 첫 접속이 응답 없이 멈추면 (예전엔 13.6초 공백) 4초 만에 포기하고
    수신 서버 주소를 다시 찾아 다음 시도로 넘어간다."""
    import asyncio

    monkeypatch.setattr(rr, "RECONNECT_TRY_S", 0.3)
    calls, addrs = [], []

    class P:
        closed = None

    async def fake_publish(self):
        calls.append(self.cfg["connect_addr"])
        if len(calls) == 1:
            await asyncio.sleep(30)  # 응답 없는 서버
        return P()

    async def fake_getaddrinfo(host, port, family=0, type=0):
        addrs.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("2.2.2.2", port))]

    monkeypatch.setattr(rr.Relay, "_publish", fake_publish)

    async def run():
        asyncio.get_running_loop().getaddrinfo = fake_getaddrinfo
        r = rr.Relay({"host": "a.rtmps.youtube.com", "port": 443, "connect_addr": "1.1.1.1"})
        t0 = time.monotonic()
        pub = await r._reconnect(rr.Feed())
        return pub, time.monotonic() - t0

    pub, took = asyncio.run(run())
    assert isinstance(pub, P) and took < 2
    assert calls == ["1.1.1.1", "2.2.2.2"]  # 두 번째 시도는 다른 서버로
    assert addrs == ["a.rtmps.youtube.com"]


def test_relay_reports_missing_audio_and_missing_input(monkeypatch):
    """내용 감시 (v2.13): 영상만 오고 소리가 8초 없으면 'no_audio' 한 번, 다시 오면 'audio_back'.
    ffmpeg 가 붙어 있는데 아무것도 안 보내면 10초·30초에 'no_input', 다시 보내면 'input_back'. 영상 교체 중(busy 아님)은 안 센다."""
    events = []
    monkeypatch.setattr(rr, "_emit", lambda e: events.append(e) if e["event"] != "stats" else None)
    r = rr.Relay({})
    r.busy = True
    r.feed = rr.Feed(r.stats)
    w = r._watch_state(0.0)
    t = 0.0

    def tick(video=30, audio=43, data=True):
        nonlocal t
        t += 1
        r.stats["v"] += video
        r.stats["a"] += audio
        if data:
            r.stats["in"] += 600_000
            r.stats["out"] += 600_000
        r._watch_step(w, t)

    for _ in range(20):
        tick()
    assert events == []
    for _ in range(7):
        tick(audio=0)
    assert events == []  # 7초까지는 조용히 (곡 사이 짧은 공백 등)
    tick(audio=0)
    assert events == [{"event": "no_audio", "sec": 8}]
    for _ in range(5):
        tick(audio=0)
    assert len(events) == 1  # 한 번만
    tick()
    assert events[-1] == {"event": "audio_back", "sec": 13}
    events.clear()
    for _ in range(9):
        tick(video=0, audio=0, data=False)
    assert events == []
    tick(video=0, audio=0, data=False)
    assert events == [{"event": "no_input", "sec": 10}]
    for _ in range(20):
        tick(video=0, audio=0, data=False)
    assert events == [{"event": "no_input", "sec": 10}, {"event": "no_input", "sec": 30}]
    tick()
    assert events[-1] == {"event": "input_back", "sec": 30}
    events.clear()
    r.busy = False  # 일일 재시작·영상 교체로 ffmpeg 가 잠깐 없는 동안
    for _ in range(40):
        tick(video=0, audio=0, data=False)
    assert events == []


def test_ingest_ips_spread_evenly(monkeypatch):
    """v2.18: 라이브를 유튜브 수신 IP 여러 개에 골고루 (예전: DNS 맨 앞 IP 하나 → 한 곳에 13개 몰림).
    DNS 답 순서가 바뀌어도 같은 라이브는 같은 IP."""
    import random
    import socket

    from livemax import tls_relay

    ips = ["142.250.117.134", "142.250.129.134", "142.250.140.134", "142.250.151.134", "142.251.29.134", "142.251.30.134"]

    def fake(host, port, fam, typ):
        order = ips[:]
        random.shuffle(order)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in order]

    monkeypatch.setattr(tls_relay.socket, "getaddrinfo", fake)
    pick = {f"live{i}": tls_relay.resolve_ipv4("a.rtmps.youtube.com", 443, spread=f"live{i}") for i in range(1, 51)}
    counts = {a: list(pick.values()).count(a) for a in ips}
    assert max(counts.values()) <= 9 and min(counts.values()) >= 8  # 50개 ÷ 6 = 8~9
    assert all(tls_relay.resolve_ipv4("a.rtmps.youtube.com", 443, spread=k) == v for k, v in pick.items())  # 순서 무관
    assert tls_relay.resolve_ipv4("a.rtmps.youtube.com", 443) in ips  # spread 없으면 예전처럼


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_relay_switches_ingest_ip_on_signal_without_restarting_ffmpeg(tmp_path: Path):
    """v2.22: 자정 옮기기 — 돌고 있는 중계기에 SIGUSR1 → 분포 파일의 가장 적게 붙은 다른 IP 로 다시 접속.
    ffmpeg 는 끝까지 안 멈추고(v2.20 은 중계기를 새로 띄우다 ffmpeg 가 옛 주소에 붙어 rc=145), 새 IP 쪽은 키프레임부터."""
    import signal as _sig

    clip = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-f", "lavfi",
                    "-i", "sine=f=440:sample_rate=48000", "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "60", "-bf", "0", "-c:a", "aac", "-shortest", str(clip)], check=True)
    sink_port, relay_port = _free_port(), _free_port()
    outs = {ip: tmp_path / f"out-{ip}.flv" for ip in ("127.0.0.1", "127.0.0.2")}
    sinks = [subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-listen", "1", "-i", f"rtmp://{ip}:{sink_port}/live2/k",
                               "-c", "copy", "-f", "flv", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for ip, out in outs.items()]
    load = tmp_path / "load.json"
    load.write_text(json.dumps({"hosts": {f"127.0.0.1:{sink_port}": {"ips": ["127.0.0.1", "127.0.0.2"],
                                                                    "load": {"127.0.0.1": 5, "127.0.0.2": 1}}}}))
    log = tmp_path / "relay.log"
    relay = subprocess.Popen([sys.executable, "-m", "livemax.rtmp_relay"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    pub = None
    try:
        time.sleep(1)
        relay.stdin.write((json.dumps({"listen_port": relay_port, "host": "127.0.0.1", "port": sink_port, "app": "live2",
                                       "key": "k", "tcurl": f"rtmp://127.0.0.1:{sink_port}/live2", "tls": False,
                                       "connect_addr": "127.0.0.1", "load_file": str(load), "log": str(log)}) + "\n").encode())
        relay.stdin.close()
        time.sleep(1.5)
        pub = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-i", str(clip), "-c", "copy", "-flush_packets", "0",
                                "-f", "flv", f"tcp://127.0.0.1:{relay_port}"])
        time.sleep(4)
        relay.send_signal(_sig.SIGUSR1)  # 옮겨
        assert pub.wait(timeout=30) == 0  # ffmpeg 는 재시작 없이 끝까지
        time.sleep(0.5)
        relay.kill()
        for s in sinks:
            s.wait(timeout=20)
    finally:
        for p in [relay, pub, *sinks]:
            if p and p.poll() is None:
                p.kill()
    text = log.read_text(encoding="utf-8")
    assert "유튜브 수신 IP 옮김 (계획): 127.0.0.1 → 127.0.0.2" in text and "계획된 옮김" in text
    events = [json.loads(x) for x in relay.stdout.read().decode().splitlines() if x.strip().startswith("{")]
    rec = [e for e in events if e.get("event") == "reconnect"]
    assert rec and rec[-1]["planned"] is True and rec[-1]["gap"] < 3
    assert any(e.get("event") == "peer" and e.get("ip") == "127.0.0.2" for e in events)
    flags = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=flags",
                            "-of", "csv=p=0", str(outs["127.0.0.2"])], capture_output=True, text=True, check=True).stdout.split()
    assert flags and flags[0].startswith("K")  # 새 IP 쪽은 키프레임부터
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(outs["127.0.0.2"]), "-f", "null", "-"], capture_output=True, text=True)
    assert dec.stderr.strip() == ""


def _stall_case(tmp_path: Path, rtt_now: dict):
    """가짜 유튜브 두 지역(127.0.0.1 = 지역 0, 127.0.0.2 = 지역 1). 송출 중 지역 0 을 얼려(SIGSTOP) 길이 막힌 상황을 만든다."""
    import signal as _sig

    clip = tmp_path / "v.mp4"  # 잡음 영상 약 66Mbps — 커널 버퍼가 큰 환경(CI, 최대 약 36MB)에서도 몇 초 안에 차서 쓰기가 막히게
    # (약 9Mbps 로는 CI 의 큰 버퍼에 30초치가 다 들어가 '막힘' 이 재현 안 됐다)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "nullsrc=s=640x360:r=30,geq=lum='random(1)*255':cb=128:cr=128,format=yuv420p", "-f", "lavfi",
                    "-i", "sine=f=440:sample_rate=48000", "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "12",
                    "-g", "30", "-bf", "0", "-c:a", "aac", "-shortest", str(clip)], check=True)
    sink_port, relay_port = _free_port(), _free_port()
    outs = {ip: tmp_path / f"out-{ip}.flv" for ip in ("127.0.0.1", "127.0.0.2")}
    sinks = {ip: subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-listen", "1", "-i", f"rtmp://{ip}:{sink_port}/live2/k",
                                   "-c", "copy", "-f", "flv", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for ip, out in outs.items()}
    load = tmp_path / "load.json"
    load.write_text(json.dumps({"hosts": {f"127.0.0.1:{sink_port}": {
        "ips": ["127.0.0.1", "127.0.0.2"], "load": {"127.0.0.1": 1, "127.0.0.2": 1}, "site": {"127.0.0.1": 0, "127.0.0.2": 1},
        "rtt_base": {"127.0.0.1": 2.0, "127.0.0.2": 9.0}, "rtt_now": rtt_now}}}))
    log = tmp_path / "relay.log"
    relay = subprocess.Popen([sys.executable, "-m", "livemax.rtmp_relay"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    pub = None
    try:
        time.sleep(1)
        relay.stdin.write((json.dumps({"listen_port": relay_port, "host": "127.0.0.1", "port": sink_port, "app": "live2",
                                       "key": "k", "tcurl": f"rtmp://127.0.0.1:{sink_port}/live2", "tls": False,
                                       "connect_addr": "127.0.0.1", "load_file": str(load), "log": str(log)}) + "\n").encode())
        relay.stdin.close()
        time.sleep(1.5)
        pub = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-i", str(clip), "-c", "copy", "-flush_packets", "0",
                                "-f", "flv", f"tcp://127.0.0.1:{relay_port}"])
        time.sleep(4)
        sinks["127.0.0.1"].send_signal(_sig.SIGSTOP)  # 지역 0 이 막힘 (받기를 멈춤)
        rc = pub.wait(timeout=90)
        time.sleep(0.5)
        relay.kill()
        sinks["127.0.0.2"].wait(timeout=30)
    finally:
        sinks["127.0.0.1"].send_signal(_sig.SIGCONT)
        for p in [relay, pub, *sinks.values()]:
            if p and p.poll() is None:
                p.kill()
    events = [json.loads(x) for x in relay.stdout.read().decode().splitlines() if x.strip().startswith("{")]
    return rc, log.read_text(encoding="utf-8"), events, outs["127.0.0.2"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_relay_moves_to_other_site_before_it_gets_bad(tmp_path: Path):
    """v2.23 C: 못 보낸 영상이 6초 넘게 쌓이고 다른 지역이 정상이면, 쓰기 시간 초과(10초)를 기다리지 않고 옮긴다.
    (11-14 02:23: 22개가 10~26초 밀리고 6개는 재접속 13~17초)"""
    rc, text, events, out2 = _stall_case(tmp_path, rtt_now={"127.0.0.1": 25.0, "127.0.0.2": 9.0})
    assert rc == 0  # ffmpeg 는 끝까지 (재시작 없음)
    # 어느 쪽이 먼저든(C: 밀림 6초 / A: 쓰기 막힘 — 비트레이트·커널 버퍼 크기에 따라 다름) 다른 지역으로 빨리 넘어가야 한다
    assert ("막히기 전에 다른 지역으로 옮김: 127.0.0.1 → 127.0.0.2" in text
            or "쓰기가 막힌 지역을 건너뜀: 127.0.0.1 → 127.0.0.2" in text), text
    rec = [e for e in events if e.get("event") == "reconnect"]
    assert rec and rec[-1]["gap"] < 3, text
    slow_end = [e for e in events if e.get("event") == "slow_end"]
    assert not slow_end or max(e["max_delay"] for e in slow_end) < 9  # 유튜브 '나쁨'(9초대) 전에 피함
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out2), "-f", "null", "-"], capture_output=True, text=True)
    assert dec.stderr.strip() == ""


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 없음")
def test_relay_stalled_write_skips_graceful_close_and_blocked_site(tmp_path: Path):
    """v2.23 A: 다른 지역도 느려 보여 미리 옮기지 못했을 때 — 쓰기가 막히면 정중히 닫느라 기다리지 않고(6.7초 낭비),
    막힌 지역을 다시 두드리지 않고(10.3초 낭비) 다른 지역으로 바로. 끊긴 순간부터 다시 붙기까지 짧게."""
    rc, text, events, out2 = _stall_case(tmp_path, rtt_now={"127.0.0.1": 25.0, "127.0.0.2": 40.0})
    assert rc == 0
    assert "막히기 전에" not in text  # 다른 지역도 느려 보임 → 미리 옮기지 않음
    assert "쓰기가 막힌 지역을 건너뜀: 127.0.0.1 → 127.0.0.2" in text, text
    rec = [e for e in events if e.get("event") == "reconnect"]
    assert rec and rec[-1]["auto"] is False and rec[-1]["gap"] < 3  # 예전: 17.5초



def test_auto_move_rule(tmp_path: Path):
    """v2.23 C 규칙: 밀림 6초 이상 + 다른 지역 지금 왕복이 정상 → 그 지역 IP 를 골라 지금 연결을 끊음.
    다른 지역도 느리면 안 옮김(우리 회선일 수 있음), 6초 미만이면 안 옮김, 한 번 옮기면 10분은 다시 안 옮김."""
    load = tmp_path / "load.json"

    def write(rtt_now):
        load.write_text(json.dumps({"hosts": {"a:443": {
            "ips": ["1.1.1.1", "2.2.2.2"], "load": {"1.1.1.1": 25, "2.2.2.2": 25}, "site": {"1.1.1.1": 0, "2.2.2.2": 1},
            "rtt_base": {"1.1.1.1": 9.0, "2.2.2.2": 2.4}, "rtt_now": rtt_now}}}))

    class Pub:
        def __init__(self):
            self.closed, self.aborted = asyncio.Event(), False

        def abort(self):
            self.aborted = True
            self.closed.set()

    r = rr.Relay({"listen_port": 0, "host": "a", "port": 443, "app": "live2", "key": "k", "tcurl": "", "tls": True,
                          "connect_addr": "1.1.1.1", "load_file": str(load)})
    r.busy, r.pub = True, Pub()
    write({"1.1.1.1": 25.0, "2.2.2.2": 2.5})
    r._maybe_auto_move(5.9, 1000.0)
    assert r.auto_target is None and not r.pub.aborted        # 6초 미만
    write({"1.1.1.1": 25.0, "2.2.2.2": 9.0})                   # 다른 지역도 기준(2.4)의 1.6배·+5ms 넘게 느림
    r._maybe_auto_move(7.0, 1000.0)
    assert r.auto_target is None and not r.pub.aborted
    write({"1.1.1.1": 25.0, "2.2.2.2": 2.6})
    r._maybe_auto_move(7.0, 1000.0)
    assert r.auto_target == "2.2.2.2" and r.pub.aborted        # 옮김
    r.auto_target, r.pub = None, Pub()
    r._maybe_auto_move(8.0, 1300.0)
    assert r.auto_target is None                               # 10분 안엔 다시 안 옮김
    r._maybe_auto_move(8.0, 1000.0 + rr.AUTO_MOVE_COOLDOWN_S + 1)
    assert r.auto_target == "2.2.2.2"
