"""새 송출 중계기(lmrelay): RTMP 조각·확장 타임스탬프·AMF·FLV 파싱, 그리고 실제 ffmpeg RTMP 서버로 중계."""

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
