"""LiveMax 송출 중계기 (lmrelay) — ffmpeg 의 잘게 쪼갠 RTMP 전송을 큰 덩어리로 묶어 CPU 를 줄인다.

문제 (live1 실측, 4코어·라이브 10개):
  ffmpeg 내장 RTMP 는 128바이트 조각마다 전송 호출을 따로 한다 (라이브 1개당 초당 약 8,000번).
  커널이 패킷은 묶어 보내지만 호출 처리 비용은 남아, ffmpeg 커널 시간 149% + stunnel 46% —
  서버 CPU 의 절반이 '작은 조각 넘기기'에 쓰였다.

해결:
  ffmpeg → (FLV, 32KB 덩어리) → 127.0.0.1 이 중계기 → (RTMP 4KB 조각을 메시지 단위로 한 번에) → 유튜브
  중계기가 RTMP 대화(handshake·connect·publish)와 TLS(RTMPS) 를 직접 한다 → stunnel 불필요.
  재인코딩은 없다: FLV 태그의 바이트를 그대로 RTMP 메시지로 옮긴다 (DESIGN 11-1 그대로).

설계:
  - 라이브마다 별도 프로세스 (하나가 죽어도 다른 라이브는 무관 — stunnel 과 같은 원칙).
  - 스트림 키는 명령줄·로그에 절대 넣지 않는다: 설정은 stdin 으로 JSON 한 번 받는다.
  - ffmpeg 연결 1개 = 유튜브 세션 1개. 어느 쪽이든 끊기면 둘 다 닫는다 → ffmpeg 가 종료 →
    supervisor 가 지금처럼 재시작한다 (재연결 정책은 그대로).
  - 유튜브가 느려지면 쓰기가 막히고 → ffmpeg 쓰기도 막혀 진행이 멈춘다 → 기존 감시(30초 정지)가 처리.
  - 24시간 송출: 타임스탬프가 0xFFFFFF(약 4시간 40분)를 넘으면 확장 타임스탬프를 쓴다 (모든 조각에).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket
import ssl
import struct
import sys
import time
from collections import deque

try:
    import fcntl
    import termios
    _OUTQ = getattr(termios, "TIOCOUTQ", 0x5411)
except ImportError:  # 윈도우 (테스트용) — 커널 대기량은 0 으로 본다
    fcntl = None
    _OUTQ = 0

log = logging.getLogger("livemax.rtmp_relay")

OUT_CHUNK = 4096  # 보내는 RTMP 조각 크기 (OBS·nginx 와 같은 값)
WRITE_TIMEOUT = 10.0  # 유튜브 쪽 쓰기가 이만큼 막히면 끊긴 것으로 보고 다시 접속
BATCH_S = 0.1  # ffmpeg 쪽에서 이만큼 모아 한 번에 보낸다
RECONNECT_GIVEUP_S = 30.0  # 유튜브 재접속을 이만큼 시도해도 안 되면 ffmpeg 재시작으로 넘긴다
RECONNECT_TRY_S = 4.0  # 재접속 한 번에 이만큼만 기다린다 (정상은 0.3~0.8초 — 응답 없는 서버를 15초씩 기다리지 않게)
KEEP_S = 20  # ffmpeg 가 바뀌는 동안(영상 교체·일일 재시작) 유튜브 연결을 이만큼 살려 둔다
MAX_BUFFER = 40 * 1024 * 1024  # 재접속 동안 쌓아 둘 최대 크기 (5Mbps 기준 약 60초)
TS_RENEW_MS = 40 * 86400 * 1000  # RTMP 시각은 32비트(약 49.7일)에서 0으로 돌아간다 → 40일 넘으면 다음 영상 교체 때 새로 접속
STATS_S = 15  # 이만큼마다 supervisor 에 전송량·지연을 알린다 (DB 기록용)
SLOW_DELAY_S = 2.0  # 유튜브로 못 보내고 밀린 영상이 이만큼(초) 넘게 이어지면 '전송 밀림'
SLOW_HOLD_S = 5  # 밀림이 이만큼 계속돼야 기록 (순간 출렁임은 무시), 회복도 이만큼 계속돼야 끝으로 봄
# 내용 감시 (v2.13): 원인이 무엇이든 사고는 '유튜브로 가는 영상·소리가 모자람'으로 나타난다 → 영상·소리를 따로 센다
NO_AUDIO_S = 8    # 영상은 오는데 소리가 이만큼(초) 한 조각도 없으면 '소리 없음' (ffmpeg 가 음악 연결을 잃은 채 영상만 보냄)
NO_INPUT_S = (10, 30)  # ffmpeg 가 붙어 있는데 이만큼(초) 아무것도 안 보내면 알림 — 10초: 기록, 30초: 송출 프로그램 재시작 요청

# ------------------------------------------------------------------ AMF0


def amf_enc(v) -> bytes:
    if v is None:
        return b"\x05"
    if isinstance(v, bool):
        return b"\x01" + (b"\x01" if v else b"\x00")
    if isinstance(v, (int, float)):
        return b"\x00" + struct.pack(">d", float(v))
    if isinstance(v, str):
        b = v.encode("utf-8")
        return b"\x02" + struct.pack(">H", len(b)) + b
    if isinstance(v, dict):
        out = [b"\x03"]
        for k, x in v.items():
            kb = k.encode("utf-8")
            out.append(struct.pack(">H", len(kb)) + kb + amf_enc(x))
        out.append(b"\x00\x00\x09")
        return b"".join(out)
    raise TypeError(f"AMF0 로 못 바꿈: {type(v)}")


def _amf_key(buf: bytes, pos: int) -> tuple[str, int]:
    n = struct.unpack_from(">H", buf, pos)[0]
    return buf[pos + 2:pos + 2 + n].decode("utf-8", "replace"), pos + 2 + n


def amf_dec(buf: bytes, pos: int = 0):
    """(값, 다음 위치). 서버 응답(_result·onStatus)에 쓰이는 타입만."""
    t = buf[pos]
    pos += 1
    if t == 0x00:
        return struct.unpack_from(">d", buf, pos)[0], pos + 8
    if t == 0x01:
        return buf[pos] != 0, pos + 1
    if t == 0x02:
        return _amf_key(buf, pos)
    if t in (0x03, 0x08):  # object, ECMA array
        if t == 0x08:
            pos += 4
        obj = {}
        while pos + 3 <= len(buf):
            if buf[pos:pos + 3] == b"\x00\x00\x09":
                return obj, pos + 3
            k, pos = _amf_key(buf, pos)
            obj[k], pos = amf_dec(buf, pos)
        return obj, pos
    if t in (0x05, 0x06):  # null, undefined
        return None, pos
    if t == 0x0A:  # strict array
        n = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        arr = []
        for _ in range(n):
            x, pos = amf_dec(buf, pos)
            arr.append(x)
        return arr, pos
    if t == 0x0C:  # long string
        n = struct.unpack_from(">I", buf, pos)[0]
        return buf[pos + 4:pos + 4 + n].decode("utf-8", "replace"), pos + 4 + n
    raise ValueError(f"모르는 AMF0 타입 0x{t:02x}")


def amf_dec_all(buf: bytes) -> list:
    out, pos = [], 0
    while pos < len(buf):
        v, pos = amf_dec(buf, pos)
        out.append(v)
    return out


# ------------------------------------------------------------------ RTMP 조각


def _basic(fmt: int, csid: int) -> bytes:
    assert 2 <= csid < 64
    return bytes([(fmt << 6) | csid])


def encode_message(csid: int, mtype: int, ts: int, msid: int, payload: bytes, chunk: int = OUT_CHUNK) -> bytes:
    """메시지 1개를 RTMP 조각들로 (첫 조각 fmt0, 나머지 fmt3). 한 번의 쓰기로 보낼 수 있게 bytes 하나로."""
    ts &= 0xFFFFFFFF
    ext = ts >= 0xFFFFFF
    head = (_basic(0, csid) + (0xFFFFFF if ext else ts).to_bytes(3, "big") + len(payload).to_bytes(3, "big")
            + bytes([mtype]) + struct.pack("<I", msid))
    ext_b = struct.pack(">I", ts) if ext else b""
    parts = [head, ext_b, payload[:chunk]]
    cont = _basic(3, csid) + ext_b  # 확장 타임스탬프는 이어지는 조각마다 반복 (Adobe 명세·ffmpeg 와 같게)
    for i in range(chunk, len(payload), chunk):
        parts += [cont, payload[i:i + chunk]]
    return b"".join(parts)


# ------------------------------------------------------------------ handshake (ffmpeg 와 같은 디지스트 방식)

_FP_KEY = b"Genuine Adobe Flash Player 001" + bytes.fromhex(
    "F0EEC24A8068BEE82E00D0D1029E7E576EEC5D2D29806FAB93B8E636CFEB31AE")
_FMS_KEY = b"Genuine Adobe Flash Media Server 001" + bytes.fromhex(
    "F0EEC24A8068BEE82E00D0D1029E7E576EEC5D2D29806FAB93B8E636CFEB31AE")


def _digest_pos(buf: bytes, off: int, mod: int, add: int) -> int:
    return sum(buf[off:off + 4]) % mod + add


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def make_c1() -> bytes:
    c1 = bytearray(struct.pack(">I", 0) + bytes([9, 0, 124, 2]) + os.urandom(1528))
    pos = _digest_pos(c1, 8, 728, 12)
    c1[pos:pos + 32] = _hmac(_FP_KEY[:30], bytes(c1[:pos] + c1[pos + 32:]))
    return bytes(c1)


def make_c2(s1: bytes) -> bytes:
    """서버 S1 에 디지스트가 있으면 서명한 C2, 없으면 S1 을 그대로 돌려준다 (단순 handshake)."""
    for off, add in ((772, 776), (8, 12)):
        pos = _digest_pos(s1, off, 728, add)
        if _hmac(_FMS_KEY[:36], s1[:pos] + s1[pos + 32:]) == s1[pos:pos + 32]:
            key = _hmac(_FP_KEY, s1[pos:pos + 32])
            c2 = os.urandom(1504)
            return c2 + _hmac(key, c2)
    return s1


# ------------------------------------------------------------------ 유튜브 쪽 세션


class RtmpError(RuntimeError):
    pass


class Publisher:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.r, self.w = reader, writer
        self.in_chunk = 128
        self.in_state: dict[int, dict] = {}
        self.ack_window = 2_500_000
        self.received = 0
        self.last_ack = 0
        self.txn = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.status: asyncio.Queue = asyncio.Queue()
        self.stream_id = 0
        self.reader_task: asyncio.Task | None = None
        self.closed = asyncio.Event()

    # --- 보내기
    async def send(self, data: bytes) -> None:
        self.w.write(data)
        await asyncio.wait_for(self.w.drain(), WRITE_TIMEOUT)

    async def command(self, name: str, *args, csid: int = 3, msid: int = 0, wait: bool = True):
        self.txn += 1
        txn = self.txn
        fut = asyncio.get_running_loop().create_future() if wait else None
        if fut:
            self.pending[txn] = fut
        await self.send(encode_message(csid, 20, 0, msid, amf_enc(name) + amf_enc(txn) + b"".join(amf_enc(a) for a in args)))
        if fut:
            return await asyncio.wait_for(fut, 15)
        return None

    # --- 받기
    async def _read(self, n: int) -> bytes:
        b = await self.r.readexactly(n)
        self.received += n
        return b

    async def read_loop(self) -> None:
        try:
            while True:
                b0 = (await self._read(1))[0]
                fmt, csid = b0 >> 6, b0 & 0x3F
                if csid == 0:
                    csid = 64 + (await self._read(1))[0]
                elif csid == 1:
                    x = await self._read(2)
                    csid = 64 + x[0] + x[1] * 256
                st = self.in_state.setdefault(csid, {"ts": 0, "delta": 0, "len": 0, "type": 0, "msid": 0,
                                                     "buf": b"", "ext": False})
                new_msg = not st["buf"]
                if fmt <= 2:
                    h = await self._read((11, 7, 3)[fmt])
                    t = int.from_bytes(h[0:3], "big")
                    if fmt <= 1:
                        st["len"], st["type"] = int.from_bytes(h[3:6], "big"), h[6]
                    if fmt == 0:
                        st["msid"] = struct.unpack("<I", h[7:11])[0]
                    st["ext"] = t == 0xFFFFFF
                    if st["ext"]:
                        t = struct.unpack(">I", await self._read(4))[0]
                    if fmt == 0:
                        st["ts"], st["delta"] = t, 0
                    else:
                        st["delta"] = t
                        st["ts"] += t
                else:
                    if st["ext"]:
                        await self._read(4)
                    if new_msg:
                        st["ts"] += st["delta"]
                n = min(self.in_chunk, st["len"] - len(st["buf"]))
                st["buf"] += await self._read(n) if n > 0 else b""
                if len(st["buf"]) >= st["len"]:
                    payload, st["buf"] = st["buf"], b""
                    await self._handle(st["type"], payload)
                if self.received - self.last_ack >= self.ack_window:
                    self.last_ack = self.received
                    await self.send(encode_message(2, 3, 0, 0, struct.pack(">I", self.received & 0xFFFFFFFF)))
        except (asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.TimeoutError) as e:
            log.info("유튜브 연결 종료: %s", type(e).__name__)
        except Exception:  # noqa: BLE001
            log.exception("유튜브 응답 처리 실패")
        finally:
            self.closed.set()
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(RtmpError("연결이 끊김"))

    async def _handle(self, mtype: int, p: bytes) -> None:
        if mtype == 1:
            self.in_chunk = struct.unpack(">I", p[:4])[0] & 0x7FFFFFFF
        elif mtype == 5:
            self.ack_window = struct.unpack(">I", p[:4])[0] or self.ack_window
        elif mtype == 6:
            await self.send(encode_message(2, 5, 0, 0, p[:4]))  # 같은 크기로 Window Ack Size 회신
        elif mtype == 4:
            ev = struct.unpack(">H", p[:2])[0]
            if ev == 6:  # PingRequest → PingResponse
                await self.send(encode_message(2, 4, 0, 0, struct.pack(">H", 7) + p[2:6]))
        elif mtype == 20:
            vals = amf_dec_all(p)
            name = vals[0] if vals else ""
            if name in ("_result", "_error") and len(vals) > 1:
                fut = self.pending.pop(int(vals[1] or 0), None)
                if fut and not fut.done():
                    if name == "_error":
                        fut.set_exception(RtmpError(_desc(vals)))
                    else:
                        fut.set_result(vals)
            elif name == "onStatus":
                info = next((v for v in vals[2:] if isinstance(v, dict)), {})
                await self.status.put(info)
            # onBWDone, onFCPublish 등은 무시

    # --- 단계
    async def handshake(self) -> None:
        c1 = make_c1()
        self.w.write(b"\x03" + c1)
        await self.w.drain()
        s0s1 = await self.r.readexactly(1537)
        if s0s1[0] != 3:
            raise RtmpError(f"RTMP 버전이 다름 ({s0s1[0]})")
        s1 = s0s1[1:]
        self.w.write(make_c2(s1))
        await self.w.drain()
        await self.r.readexactly(1536)  # S2

    async def connect(self, app: str, tcurl: str) -> None:
        await self.send(encode_message(2, 1, 0, 0, struct.pack(">I", OUT_CHUNK)))  # Set Chunk Size
        self.reader_task = asyncio.create_task(self.read_loop())
        await self.command("connect", {"app": app, "type": "nonprivate",
                                       "flashVer": "FMLE/3.0 (compatible; LiveMax)", "tcUrl": tcurl})

    async def publish(self, key: str) -> None:
        await self.command("releaseStream", None, key, wait=False)
        await self.command("FCPublish", None, key, wait=False)
        res = await self.command("createStream", None)
        sid = next((v for v in res[3:] if isinstance(v, float)), None) if res else None
        self.stream_id = int(sid or 1)
        await self.command("publish", None, key, "live", csid=8, msid=self.stream_id, wait=False)
        deadline = time.monotonic() + 15
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RtmpError("방송 시작 응답(Publish.Start) 없음")
            info = await asyncio.wait_for(self.status.get(), left)
            code = str(info.get("code", ""))
            if code == "NetStream.Publish.Start":
                return
            if "Error" in code or "Failed" in code or "BadName" in code or "Rejected" in code:
                raise RtmpError(f"방송 시작 거부: {code} {info.get('description', '')}")

    def tag_bytes(self, tag_type: int, ts: int, data: bytes) -> bytes:
        if tag_type == 18:  # 메타데이터 → @setDataFrame (ffmpeg 와 같게)
            return encode_message(5, 18, ts, self.stream_id, _SET_DATA_FRAME + data)
        if tag_type == 8:
            return encode_message(4, 8, ts, self.stream_id, data)
        if tag_type == 9:
            return encode_message(6, 9, ts, self.stream_id, data)
        return b""

    async def send_media(self, data: bytes) -> None:
        """미디어는 모아서 한 번에 쓰고, 보낼 버퍼가 많이 쌓였을 때만 기다린다 (메시지마다 기다리면 CPU 낭비)."""
        self.w.write(data)
        if self.w.transport.get_write_buffer_size() > 256 * 1024:
            await asyncio.wait_for(self.w.drain(), WRITE_TIMEOUT)

    def abort(self) -> None:
        if self.reader_task:
            self.reader_task.cancel()
        self.closed.set()
        try:
            self.w.transport.abort()
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        try:
            if self.stream_id and not self.closed.is_set():
                await self.command("FCUnpublish", None, "", wait=False)
                await self.command("deleteStream", None, float(self.stream_id), wait=False)
        except Exception:  # noqa: BLE001
            pass
        if self.reader_task:
            self.reader_task.cancel()
        self.w.close()
        try:
            await asyncio.wait_for(self.w.wait_closed(), 3)
        except Exception:  # noqa: BLE001
            pass


_SET_DATA_FRAME = amf_enc("@setDataFrame")


def _desc(vals: list) -> str:
    info = next((v for v in vals[2:] if isinstance(v, dict)), {})
    return f"{info.get('code', '')} {info.get('description', '')}".strip() or "오류"


# ------------------------------------------------------------------ ffmpeg 쪽 (FLV 받기)


async def read_flv_header(r: asyncio.StreamReader) -> None:
    h = await r.readexactly(9)
    if h[:3] != b"FLV":
        raise RtmpError("ffmpeg 출력이 FLV 가 아님")
    skip = struct.unpack(">I", h[5:9])[0] - 9
    if skip > 0:
        await r.readexactly(skip)
    await r.readexactly(4)  # PreviousTagSize0


class FlvParser:
    """받은 바이트를 모아 완성된 태그만 꺼낸다 (태그마다 readexactly 를 3번 부르지 않게)."""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        self.buf += data
        out, b, p = [], self.buf, self.pos
        while len(b) - p >= 15:
            size = int.from_bytes(b[p + 1:p + 4], "big")
            end = p + 11 + size + 4
            if len(b) < end:
                break
            ts = int.from_bytes(b[p + 4:p + 7], "big") | (b[p + 7] << 24)
            out.append((b[p] & 0x1F, ts, bytes(b[p + 11:p + 11 + size])))
            p = end
        if p > 1 << 20 or p == len(b):
            del b[:p]
            p = 0
        self.pos = p
        return out


async def read_flv_tag(r: asyncio.StreamReader) -> tuple[int, int, bytes]:
    h = await r.readexactly(11)
    size = int.from_bytes(h[1:4], "big")
    ts = int.from_bytes(h[4:7], "big") | (h[7] << 24)
    data = await r.readexactly(size)
    await r.readexactly(4)  # PreviousTagSize
    return h[0] & 0x1F, ts, data


# ------------------------------------------------------------------ 중계 본체


def _emit(obj: dict) -> None:
    """supervisor 에 알릴 일(재접속 등)을 표준출력 한 줄 JSON 으로."""
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _avc_seq(t: int, d: bytes) -> bool:
    return t == 9 and len(d) > 1 and (d[0] & 0x0F) == 7 and d[1] == 0


def _aac_seq(t: int, d: bytes) -> bool:
    return t == 8 and len(d) > 1 and (d[0] >> 4) == 10 and d[1] == 0


def _keyframe(t: int, d: bytes) -> bool:
    return t == 9 and len(d) > 1 and (d[0] >> 4) == 1 and not _avc_seq(t, d)


class Feed:
    """ffmpeg 쪽을 계속 읽어 태그를 쌓는다 — 유튜브가 끊겨 재접속하는 동안에도 ffmpeg 는 막히지 않는다."""

    def __init__(self, stats: dict | None = None):
        self.q: deque = deque()
        self.size = 0
        self.eof = False
        self.stats = stats if stats is not None else {"in": 0}
        self.meta = self.avc = self.aac = None

    async def pump(self, fr: asyncio.StreamReader) -> None:
        try:
            await read_flv_header(fr)
            parser = FlvParser()
            while True:
                chunk = await fr.read(256 * 1024)
                if not chunk:
                    break
                self.stats["in"] += len(chunk)
                for t, ts, d in parser.feed(chunk):
                    if t == 8:
                        self.stats["a"] = self.stats.get("a", 0) + 1
                    elif t == 9:
                        self.stats["v"] = self.stats.get("v", 0) + 1
                    if t == 18 and self.meta is None:
                        self.meta = (t, ts, d)
                    elif _avc_seq(t, d):
                        self.avc = (t, ts, d)
                    elif _aac_seq(t, d):
                        self.aac = (t, ts, d)
                    self.q.append((t, ts, d))
                    self.size += len(d)
                if self.size > MAX_BUFFER:  # 오래 끊겨 너무 쌓이면 최근 키프레임부터만 남긴다
                    self.rewind_to_keyframe()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, RtmpError):
            pass
        finally:
            self.eof = True

    def take(self) -> list:
        out = list(self.q)
        self.q.clear()
        self.size = 0
        return out

    def rewind_to_keyframe(self) -> int:
        """쌓인 것 중 가장 최근 키프레임 앞은 버린다. 그 키프레임의 시각을 돌려준다 (없으면 첫 태그 시각)."""
        q = list(self.q)
        idx = next((i for i in range(len(q) - 1, -1, -1) if _keyframe(q[i][0], q[i][2])), None)
        if idx is None:
            return q[0][1] if q else 0
        base = q[idx][1]
        rest = [x for x in q[idx:] if x[0] == 9 or x[1] >= base]  # 키프레임보다 이른 소리는 버림
        self.q = deque(rest)
        self.size = sum(len(x[2]) for x in rest)
        return base

    def headers(self) -> list:
        return [x for x in (self.meta, self.avc, self.aac) if x]


class Relay:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.busy = False
        self.pub: Publisher | None = None  # 유튜브 연결 — ffmpeg 가 바뀌어도(영상 교체) 살려 둔다
        self.last_out = 0  # 마지막으로 보낸 시각(ms)
        self.last_wall = 0.0  # 그때의 실제 시각
        self.idle_task: asyncio.Task | None = None
        # ffmpeg 에서 받은 바이트 / 유튜브 쪽에 넘긴 바이트 / 받은 소리·영상 조각 수 (누적)
        self.stats = {"in": 0, "out": 0, "a": 0, "v": 0}
        self.feed: Feed | None = None
        self.peer = "?"  # 지금 붙은 유튜브 수신 서버 IP (로그용)
        self.reconnecting = False

    def _pending(self) -> tuple[int, int, int]:
        """유튜브로 아직 못 나간 바이트 (중계기 버퍼 + 보내기 버퍼, 커널 송신 대기열).
        커널 송신 버퍼가 차 있으면 '서버→유튜브 길이 느림', 비어 있는데 중계기에만 쌓였으면 '서버가 늦게 처리'.
        돌려주는 값: (중계기 쪽, 커널 대기, 커널 송신 버퍼 크기)"""
        app = self.feed.size if self.feed else 0
        kern = cap = 0
        pub = self.pub
        if pub is not None and not pub.closed.is_set():
            try:
                app += pub.w.transport.get_write_buffer_size()
            except Exception:  # noqa: BLE001
                pass
            sock = pub.w.get_extra_info("socket")
            if fcntl is not None and sock is not None:
                try:
                    kern = struct.unpack("i", fcntl.ioctl(sock.fileno(), _OUTQ, b"\0\0\0\0"))[0]
                    cap = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) // 2  # 리눅스는 2배로 알려 줌
                except (OSError, ValueError):
                    pass
        return app, kern, cap

    async def watch(self) -> None:
        """1초마다 '유튜브로 밀린 영상(초)'을 재고, 15초마다 전송량을 알린다. 문제 있을 때만 로그 (밀림 시작·끝)."""
        w = self._watch_state(time.monotonic())
        while True:
            await asyncio.sleep(1)
            self._watch_step(w, time.monotonic())

    def _watch_state(self, now: float) -> dict:
        return {"t": now, "in": self.stats["in"], "out": self.stats["out"], "rate": 0.0, "win": deque(maxlen=STATS_S),
                "slow": None, "calm": 0, "emit": now,
                "a": self.stats.get("a", 0), "v": self.stats.get("v", 0), "mute": 0.0, "mute_on": False,
                "idle": 0.0, "idle_lv": 0}

    def _watch_content(self, w: dict, dt: float, d_in: int) -> None:
        """영상·소리가 실제로 오는지. 영상만 오고 소리가 없으면 'no_audio', 아무것도 안 오면 'no_input' (송출 중일 때만)."""
        a, v = self.stats.get("a", 0), self.stats.get("v", 0)
        da, dv = a - w["a"], v - w["v"]
        w["a"], w["v"] = a, v
        if not self.busy:  # ffmpeg 가 바뀌는 중(영상 교체·재시작) — 세지 않는다
            w["mute"] = w["idle"] = 0.0
            w["mute_on"], w["idle_lv"] = False, 0
            return
        # --- 소리 없음: 영상 조각은 오는데 소리 조각이 0
        if da > 0:
            if w["mute_on"]:
                log.warning("소리가 다시 들어옴 (%.0f초 만에)", w["mute"])
                _emit({"event": "audio_back", "sec": round(w["mute"])})
            w["mute"], w["mute_on"] = 0.0, False
        elif dv > 0:
            w["mute"] += dt
            if not w["mute_on"] and w["mute"] >= NO_AUDIO_S:
                w["mute_on"] = True
                log.warning("소리 없음 — 영상만 들어온 지 %.0f초 (송출 프로그램이 음악 연결을 잃음)", w["mute"])
                _emit({"event": "no_audio", "sec": round(w["mute"])})
        # --- 입력 없음: ffmpeg 가 붙어 있는데 아무것도 안 보냄
        if d_in > 0:
            if w["idle_lv"]:
                log.warning("송출 프로그램이 다시 보내기 시작 (%.0f초 동안 멈춤)", w["idle"])
                _emit({"event": "input_back", "sec": round(w["idle"])})
            w["idle"], w["idle_lv"] = 0.0, 0
        else:
            w["idle"] += dt
            if w["idle_lv"] < len(NO_INPUT_S) and w["idle"] >= NO_INPUT_S[w["idle_lv"]]:
                w["idle_lv"] += 1
                log.warning("송출 프로그램이 %.0f초째 아무것도 안 보냄", w["idle"])
                _emit({"event": "no_input", "sec": round(w["idle"])})

    def _watch_step(self, w: dict, now: float) -> None:
        dt = max(0.001, now - w["t"])
        app, kern, cap = self._pending()
        pend = app + kern
        wire = self.stats["out"] - pend + (self.feed.size if self.feed else 0)  # 실제로 소켓을 떠난 누적 바이트
        d_in, d_out = self.stats["in"] - w["in"], max(0, wire - w["out"])
        w["t"], w["in"], w["out"] = now, self.stats["in"], wire
        if d_in > 0:  # 최근 입력 속도 (바이트/초) — 밀린 양을 '초'로 바꿀 때 쓴다
            w["rate"] = d_in / dt if w["rate"] == 0 else w["rate"] * 0.9 + d_in / dt * 0.1
        w["win"].append((dt, d_in, d_out))
        self._watch_content(w, dt, d_in)
        delay = pend / w["rate"] if w["rate"] > 0 and self.busy else 0.0
        if self.busy and dt >= 2.5:  # 1초마다 깨어나야 하는데 한참 늦음 = 중계기 자체가 멈춤 (그동안 유튜브로 아무것도 안 감)
            log.warning("중계기가 %.1f초 멈췄다가 다시 움직임 (서버 처리 지연)", dt - 1)
            _emit({"event": "stall", "sec": round(dt - 1, 1)})
        slow = w["slow"]
        # --- 밀림 판정 (재접속 중은 이미 따로 기록하므로 제외)
        if self.busy and not self.reconnecting and delay >= SLOW_DELAY_S:
            w["calm"] = 0
            if slow is None:
                slow = w["slow"] = {"t0": now, "max": delay, "in": 0, "out": 0, "n": 0, "logged": False,
                                    "kern": 0, "app": 0, "lag": 0.0, "fill": 0.0}
            slow["max"] = max(slow["max"], delay)
            slow["kern"], slow["app"] = max(slow["kern"], kern), max(slow["app"], app)
            slow["fill"] = max(slow["fill"], kern / cap if cap else 0.0)
            slow["lag"] = max(slow["lag"], dt - 1.0)  # 1초마다 깨어나야 하는데 늦었다 = 중계기 자체가 멈칫
            slow["in"] += d_in
            slow["out"] += d_out
            slow["n"] += dt
            if not slow["logged"] and now - slow["t0"] >= SLOW_HOLD_S:
                slow["logged"] = True
                log.warning("유튜브 전송 밀림 시작 — 못 보낸 영상 %.1f초 쌓임 (수신 서버 %s)", delay, self.peer)
                _emit({"event": "slow_start", "delay": round(delay, 1)})
        elif slow is not None:
            w["calm"] += 1
            if w["calm"] >= SLOW_HOLD_S or not self.busy:
                if slow["logged"]:
                    span = max(1.0, slow["n"])
                    kin, kout = slow["in"] * 8 / span / 1000, slow["out"] * 8 / span / 1000
                    log.warning("유튜브 전송 밀림 끝 — %.0f초 동안, 최대 %.1f초 밀림, 유튜브로 %.0f kbps (입력 %.0f kbps, 수신 서버 %s)",
                                span, slow["max"], kout, kin, self.peer)
                    # 막힌 곳: 커널 대기열이 컸으면 회선, 중계기가 늦게 깨어났으면 서버(CPU 등)
                    where = "network" if slow["fill"] >= 0.3 and slow["lag"] < 1.0 else "server"
                    _emit({"event": "slow_end", "dur": round(span), "max_delay": round(slow["max"], 1),
                           "kbps_out": round(kout), "kbps_in": round(kin), "where": where,
                           "kern_kb": slow["kern"] // 1024, "app_kb": slow["app"] // 1024, "lag": round(slow["lag"], 1)})
                w["slow"], w["calm"] = None, 0
        # --- 15초마다 알림 (DB 기록용)
        if now - w["emit"] >= STATS_S:
            w["emit"] = now
            span = sum(x[0] for x in w["win"]) or 1
            _emit({"event": "stats", "kbps_in": round(sum(x[1] for x in w["win"]) * 8 / span / 1000),
                   "kbps_out": round(sum(x[2] for x in w["win"]) * 8 / span / 1000), "delay": round(delay, 2)})

    async def _open_upstream(self):
        c = self.cfg
        ctx = None
        if c.get("tls"):
            ctx = ssl.create_default_context(cafile=c.get("cafile") or None)
            if not c.get("verify", True):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        r, w = await asyncio.wait_for(asyncio.open_connection(
            c.get("connect_addr") or c["host"], int(c["port"]), ssl=ctx,
            server_hostname=c["host"] if ctx else None), 15)
        peer = w.get_extra_info("peername")
        self.peer = peer[0] if peer else "?"  # 붙은 유튜브 수신 서버 IP — 로그에 남겨 같은 서버 문제인지 가려낸다 (v2.17)
        sock = w.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        return r, w

    async def _publish(self) -> Publisher:
        c = self.cfg
        ur, uw = await self._open_upstream()
        pub = Publisher(ur, uw)
        try:
            await asyncio.wait_for(pub.handshake(), 15)
            await pub.connect(c["app"], c["tcurl"])
            await pub.publish(c["key"])
        except BaseException:
            pub.abort()  # 접속 도중 실패·시간 초과: 인사 없이 바로 끊는다 (응답 없는 서버를 더 기다리지 않게)
            raise
        return pub

    async def _reconnect(self, feed: "Feed") -> Publisher | None:
        """유튜브가 끊었을 때: ffmpeg 는 그대로 두고 중계기만 다시 접속 (그동안 들어온 영상은 버퍼에).

        한 번 시도는 RECONNECT_TRY_S 까지만 기다리고, 실패하면 유튜브 수신 서버 주소를 다시 찾아
        (같은 서버가 응답이 없으면 다른 서버로) 곧바로 다시 시도한다."""
        t0 = time.monotonic()
        for i, delay in enumerate((0, 0, 0.5, 1, 1, 2, 3, 5, 5, 5)):
            if feed.eof:
                return None
            if delay:
                await asyncio.sleep(delay)
            if time.monotonic() - t0 > RECONNECT_GIVEUP_S:
                break
            try:
                return await asyncio.wait_for(self._publish(), RECONNECT_TRY_S)
            except (RtmpError, ssl.SSLError, ConnectionError, OSError, asyncio.TimeoutError) as e:
                log.warning("유튜브 재접속 %d회째 실패: %s", i + 1, type(e).__name__ if isinstance(e, asyncio.TimeoutError) else e)
                await self._reresolve()
        return None

    async def _reresolve(self) -> None:
        """IPv4 고정 주소를 쓰는 경우 유튜브 수신 서버 주소를 다시 찾는다 (다른 주소가 있으면 그쪽으로)."""
        c = self.cfg
        if not c.get("connect_addr"):
            return
        try:
            infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                c["host"], int(c["port"]), family=socket.AF_INET, type=socket.SOCK_STREAM), 3)
        except (OSError, asyncio.TimeoutError):
            return
        addrs = list(dict.fromkeys(i[4][0] for i in infos))
        other = [a for a in addrs if a != c["connect_addr"]]
        if other or addrs:
            new = (other or addrs)[0]
            if new != c["connect_addr"]:
                log.info("유튜브 수신 서버 주소 바꿈 (다시 찾음)")
            c["connect_addr"] = new

    async def _close_idle(self) -> None:
        """ffmpeg 가 끊긴 뒤 KEEP_S 안에 다시 안 오면 (라이브 정지 등) 유튜브 연결을 닫는다."""
        try:
            await asyncio.sleep(KEEP_S)
        except asyncio.CancelledError:
            return
        if not self.busy and self.pub:
            log.info("ffmpeg 가 %d초 동안 다시 오지 않아 유튜브 연결을 닫음", KEEP_S)
            await self.pub.close()
            self.pub = None

    async def handle(self, fr: asyncio.StreamReader, fw: asyncio.StreamWriter) -> None:
        if self.busy:  # 라이브 1개 = ffmpeg 1개. 겹치면 새 것은 거절
            fw.close()
            return
        self.busy = True
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None
        t0 = time.monotonic()
        sent = 0
        feed = Feed(self.stats)
        self.feed = feed
        reader = asyncio.create_task(feed.pump(fr))
        keep = False  # 끝날 때 유튜브 연결을 살려 둘지 (ffmpeg 만 바뀌는 경우)
        try:
            c = self.cfg
            if self.pub is not None and not self.pub.closed.is_set() and self.last_out >= TS_RENEW_MS:
                log.info("유튜브 연결이 40일 넘게 이어짐 — 시각이 한 바퀴 돌기 전에 이번 교체 때 새로 접속")
                await self.pub.close()
                self.pub = None
            if self.pub is not None and not self.pub.closed.is_set():
                # 영상 교체·일일 재시작: ffmpeg 만 새로 왔다 → 같은 유튜브 연결에 이어 붙인다 (재접속·방송 시작 신호 없음)
                gap = time.monotonic() - self.last_wall
                base_out = self.last_out + max(33, int(gap * 1000))  # 실제 흐른 시간만큼 시각을 이어 붙임
                log.info("송출 프로그램 교체 — 유튜브 연결 유지, %.1f초 만에 이어서 송출", gap)
            else:
                if self.pub:
                    await self.pub.close()
                self.pub = await self._publish()
                base_out = 0
                self.last_out = 0  # 새 연결은 시각 0부터
                log.info("유튜브 송출 시작 (%s:%s%s, 수신 서버 %s, %.2f초)", c["host"], c["port"], " TLS" if c.get("tls") else "",
                         self.peer, time.monotonic() - t0)
            first_in = None
            while True:
                pub = self.pub
                # --- 보내기: 버퍼에 쌓인 태그를 모아 한 번에 (100ms 마다 깨어남 — CPU 절약)
                while not pub.closed.is_set():
                    tags = feed.take()
                    if tags:
                        if first_in is None:
                            first_in = tags[0][1]
                        parts = []
                        for t, ts, d in tags:
                            o = max(0, base_out + ts - first_in)
                            parts.append(pub.tag_bytes(t, o, d))
                            if o > self.last_out:
                                self.last_out = o
                        out = b"".join(parts)
                        self.stats["out"] += len(out)
                        try:
                            await pub.send_media(out)
                        except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError) as e:
                            log.warning("유튜브 쓰기 실패: %s", type(e).__name__)
                            break
                        self.last_wall = time.monotonic()
                        sent += len(out)
                    elif feed.eof:
                        log.info("ffmpeg 연결 종료 — 유튜브 연결은 잠시 유지 (영상 교체 대비)")
                        keep = True
                        return
                    await asyncio.sleep(BATCH_S)
                # --- 유튜브가 끊음 → 빠른 재접속
                cut = time.monotonic()
                log.warning("유튜브 쪽 연결이 끊김 — 중계기만 바로 다시 접속 (ffmpeg 는 계속)")
                await pub.close()
                self.reconnecting = True
                try:
                    self.pub = await self._reconnect(feed)
                finally:
                    self.reconnecting = False
                if self.pub is None:
                    log.warning("재접속 실패 — ffmpeg 연결도 닫음 (송출 프로그램 재시작으로 넘김)")
                    return
                # 다시 붙으면: 영상·소리 설정 정보를 먼저 보내고, 가장 최근 키프레임부터 이어서 (새 연결이라 시각은 0부터)
                first_in = feed.rewind_to_keyframe()
                base_out = 0
                self.last_out = 0
                await self.pub.send_media(b"".join(self.pub.tag_bytes(t, 0, d) for t, _ts, d in feed.headers()))
                gap = time.monotonic() - cut
                log.warning("유튜브 재접속 성공 — 공백 %.1f초 (수신 서버 %s)", gap, self.peer)
                _emit({"event": "reconnect", "gap": round(gap, 2)})
        except asyncio.IncompleteReadError:
            log.info("ffmpeg 연결 종료")
            keep = True
        except asyncio.TimeoutError:
            log.warning("유튜브 응답·쓰기 시간 초과 — 세션 닫음")
        except (RtmpError, ssl.SSLError, ConnectionError, OSError) as e:
            log.warning("송출 세션 실패: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("송출 세션 오류")
        finally:
            log.info("세션 종료 (%.0f초, %.1f MB)", time.monotonic() - t0, sent / 1e6)
            reader.cancel()
            fw.close()
            self.feed = None
            if keep and self.pub is not None and not self.pub.closed.is_set():
                self.idle_task = asyncio.create_task(self._close_idle())
            elif self.pub is not None:
                await self.pub.close()
                self.pub = None
            self.busy = False

    async def serve(self) -> None:
        srv = await asyncio.start_server(self.handle, "127.0.0.1", int(self.cfg["listen_port"]))
        log.info("중계 대기 127.0.0.1:%s → %s:%s", self.cfg["listen_port"], self.cfg["host"], self.cfg["port"])
        watcher = asyncio.create_task(self.watch())
        try:
            async with srv:
                await srv.serve_forever()
        finally:
            watcher.cancel()


class _HideKey(logging.Filter):
    def __init__(self, key: str):
        super().__init__()
        self.key = key

    def filter(self, record: logging.LogRecord) -> bool:
        if self.key and len(self.key) >= 6:
            msg = record.getMessage()
            if self.key in msg:
                record.msg, record.args = msg.replace(self.key, "****" + self.key[-4:]), ()
        return True


def main() -> None:
    cfg = json.loads(sys.stdin.readline())  # 스트림 키는 여기로만 받는다 (명령줄·환경변수 X)
    logging.basicConfig(filename=cfg.get("log") or None, level=logging.INFO,
                        format="%(asctime)s %(levelname)s [lmrelay] %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(_HideKey(str(cfg.get("key") or "")))
    try:
        asyncio.run(Relay(cfg).serve())
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ supervisor 쪽: 프로세스 관리


class RelayProc:
    """라이브 1개의 중계기 자식 프로세스 (tls_relay.Relay 와 같은 모양: start/alive/stop)."""

    def __init__(self, live_id: str, log_dir, on_event=None):
        self.live_id = live_id
        self.on_event = on_event  # 중계기가 알려 오는 일 (예: {"event": "reconnect", "gap": 1.2})
        self.sig = None  # (주소, 키) — 바뀌면 새로 띄운다
        self.log_path = log_dir / f"tls-{live_id}.log"  # 웹의 'TLS' 로그 자리를 그대로 쓴다
        self.proc = None
        self.port: int | None = None

    def start(self, base_url: str, key: str, verify: bool = True, ipv4: bool = True, wait_s: float = 5.0) -> int | None:
        import subprocess
        from urllib.parse import urlparse

        from . import tls_relay

        u = urlparse(base_url.rstrip("/"))
        if u.scheme not in ("rtmp", "rtmps") or not u.hostname:
            return None
        tls = u.scheme == "rtmps"
        host, port = u.hostname, u.port or (443 if tls else 1935)
        app = u.path.strip("/") or "live2"
        self.port = tls_relay.free_port()
        cfg = {"listen_port": self.port, "host": host, "port": port, "app": app, "key": key,
               "tcurl": f"{u.scheme}://{host}:{port}/{app}", "tls": tls, "verify": verify,
               "cafile": tls_relay.CA_FILE if os.path.exists(tls_relay.CA_FILE) else None,
               "connect_addr": tls_relay.resolve_ipv4(host, port) if ipv4 else None, "log": str(self.log_path)}
        self.proc = subprocess.Popen([sys.executable, "-m", "livemax.rtmp_relay"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        import threading

        threading.Thread(target=self._read_events, args=(self.proc,), name=f"{self.live_id}-relay-ev", daemon=True).start()
        try:
            self.proc.stdin.write((json.dumps(cfg) + "\n").encode())
            self.proc.stdin.close()
        except OSError:
            pass
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            if tls_relay.is_listening(self.port):
                log.info("%s: 새 중계기 시작 (pid %d, 127.0.0.1:%d → %s:%d%s)", self.live_id, self.proc.pid,
                         self.port, host, port, " TLS" if tls else "")
                return self.port
            time.sleep(0.1)
        log.warning("%s: 새 중계기를 띄우지 못함 — 기존 방식으로 송출 (로그: %s)", self.live_id, self.log_path)
        self.stop()
        return None

    def _read_events(self, proc) -> None:
        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if self.on_event:
                try:
                    self.on_event(ev)
                except Exception:  # noqa: BLE001
                    log.exception("중계기 알림 처리 실패")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        import subprocess

        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


if __name__ == "__main__":
    main()
