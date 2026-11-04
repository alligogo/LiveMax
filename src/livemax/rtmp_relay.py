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

log = logging.getLogger("livemax.rtmp_relay")

OUT_CHUNK = 4096  # 보내는 RTMP 조각 크기 (OBS·nginx 와 같은 값)
WRITE_TIMEOUT = 20.0
BATCH_S = 0.1  # ffmpeg 쪽에서 이만큼 모아 한 번에 보낸다  # 유튜브 쪽 쓰기가 이만큼 막히면 세션을 닫는다

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


class Relay:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.busy = False

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
        sock = w.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        return r, w

    async def handle(self, fr: asyncio.StreamReader, fw: asyncio.StreamWriter) -> None:
        if self.busy:  # 라이브 1개 = ffmpeg 1개. 겹치면 새 것은 거절
            fw.close()
            return
        self.busy = True
        pub = None
        t0 = time.monotonic()
        sent = 0
        try:
            c = self.cfg
            ur, uw = await self._open_upstream()
            pub = Publisher(ur, uw)
            await asyncio.wait_for(pub.handshake(), 15)
            await pub.connect(c["app"], c["tcurl"])
            await pub.publish(c["key"])
            log.info("유튜브 송출 시작 (%s:%s%s, %.2f초)", c["host"], c["port"], " TLS" if c.get("tls") else "",
                     time.monotonic() - t0)
            await read_flv_header(fr)
            parser = FlvParser()
            while not pub.closed.is_set():
                chunk = await fr.read(256 * 1024)
                if not chunk:
                    log.info("ffmpeg 연결 종료")
                    break
                tags = parser.feed(chunk)
                if tags:
                    out = b"".join(pub.tag_bytes(t, ts, d) for t, ts, d in tags)
                    await pub.send_media(out)
                    sent += len(out)
                # 40ms 씩 모아서 처리 — 깨어나는 횟수를 줄여 CPU 절약 (지연 40ms 는 송출에 영향 없음)
                await asyncio.sleep(BATCH_S)
            else:
                log.warning("유튜브 쪽 연결이 끊김 — ffmpeg 연결도 닫음")
        except asyncio.IncompleteReadError:
            log.info("ffmpeg 연결 종료")
        except asyncio.TimeoutError:
            log.warning("유튜브 응답·쓰기 시간 초과 — 세션 닫음")
        except (RtmpError, ssl.SSLError, ConnectionError, OSError) as e:
            log.warning("송출 세션 실패: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("송출 세션 오류")
        finally:
            log.info("세션 종료 (%.0f초, %.1f MB)", time.monotonic() - t0, sent / 1e6)
            if pub:
                await pub.close()
            fw.close()
            self.busy = False

    async def serve(self) -> None:
        srv = await asyncio.start_server(self.handle, "127.0.0.1", int(self.cfg["listen_port"]))
        log.info("중계 대기 127.0.0.1:%s → %s:%s", self.cfg["listen_port"], self.cfg["host"], self.cfg["port"])
        async with srv:
            await srv.serve_forever()


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

    def __init__(self, live_id: str, log_dir):
        self.live_id = live_id
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
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
