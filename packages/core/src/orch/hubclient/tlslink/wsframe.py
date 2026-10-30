"""The WebSocket profile of the hub link (orch-hub ``contracts/hub/protocol.md`` 10.10, R6 #1594): an RFC 6455 subset
that carries the bytes of the link's own pinned TLS 1.3 and nothing else. Pure codec, no socket; written from the
protocol text (D79: no hub import) and replayed against ``vectors_link_ws.json`` in
``tests/hub/test_link_ws_vectors.py``.

- Upgrade: :func:`build_request` (``GET /orch/link/1``, subprotocol ``orch-link.1``; no Origin, cookie,
  authorization or extension), :func:`check_response` (exactly 101, the right accept, ``orch-link.1``, no extension;
  a redirect is a failure and is never followed).
- Frames: :func:`encode_frame` (a client always masks with a fresh random key), :func:`decode_frame` (refusals carry
  the close code: 1002 protocol, 1003 text, 1007 bad close reason, 1009 too big; an oversize length is refused from the
  header alone) and :class:`Receiver`, which turns one direction into a byte stream and answers pings.

Nothing here is a security layer: the WebSocket (and any outer TLS) only moves inner TLS records."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "IDLE_CLOSE_S", "LINK_PATH", "MAX_CONTROL_PER_READ", "MAX_CONTROL_PER_S", "MAX_FRAME", "MAX_HEAD",
    "MAX_SEND_CHUNK", "PING_AFTER_S", "SUBPROTOCOL",
    "Frame", "HandshakeFailed", "Receiver", "WsError", "accept_key", "build_request", "check_response",
    "decode_frame", "encode_frame", "new_key",
]  # fmt: skip

LINK_PATH = "/orch/link/1"
SUBPROTOCOL = "orch-link.1"
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 262144  # largest payload accepted in one frame
MAX_SEND_CHUNK = 65536  # largest payload this side puts in one data frame
MAX_CONTROL = 125
MAX_HEAD = 8192  # the upgrade answer head, CRLF CRLF included
PING_AFTER_S = 25  # ping after this long without sending
IDLE_CLOSE_S = 75  # close after this long without receiving a byte
CLOSE_WAIT_S = 1

CONT, TEXT, BINARY, CLOSE, PING, PONG = 0, 1, 2, 8, 9, 10
_OPS = (CONT, BINARY, CLOSE, PING, PONG)
_CLOSE_OK = {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}


class WsError(Exception):
    """A refused frame: send a close with ``code`` and drop the link."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class HandshakeFailed(Exception):
    """The upgrade answer is not the one the profile allows; that address failed (no retry, no redirect)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Frame:
    fin: bool
    opcode: int
    payload: bytes


def _xor(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    k = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(len(data), "big")


def encode_frame(opcode: int, payload: bytes, *, mask: bytes | None = None, fin: bool = True) -> bytes:
    """One frame, shortest length form. Raises ``ValueError`` for anything a sender of this profile never sends."""
    if opcode not in _OPS or (mask is not None and len(mask) != 4) or len(payload) > MAX_FRAME:
        raise ValueError("frame")
    if opcode >= CLOSE and (not fin or len(payload) > MAX_CONTROL):
        raise ValueError("control frame")
    n = len(payload)
    first = bytes([opcode | (0x80 if fin else 0)])
    mbit = 0x80 if mask is not None else 0
    if n <= 125:
        head = first + bytes([mbit | n])
    elif n <= 0xFFFF:
        head = first + bytes([mbit | 126]) + n.to_bytes(2, "big")
    else:
        head = first + bytes([mbit | 127]) + n.to_bytes(8, "big")
    return head + payload if mask is None else head + mask + _xor(payload, mask)


def decode_frame(buf: bytes, *, sender: str, max_payload: int = MAX_FRAME) -> tuple[Frame, int] | None:
    """One frame from the start of ``buf`` written by ``sender`` (``client`` frames are masked, ``server`` frames not);
    ``None`` when more bytes are needed. Order of the checks: RSV, text, unknown opcode, mask bit, control frame
    (fragmented or > 125), non-shortest or top-bit length, over ``max_payload``, then the close payload."""
    if len(buf) < 2:
        return None
    if buf[0] & 0x70:
        raise WsError(1002, "rsv")
    fin, op = buf[0] >= 0x80, buf[0] & 0x0F
    if op == TEXT:
        raise WsError(1003, "text")
    if op not in _OPS:
        raise WsError(1002, "opcode")
    masked = buf[1] >= 0x80
    if masked is not (sender == "client"):
        raise WsError(1002, "mask")
    n = buf[1] & 0x7F
    if op >= CLOSE and (not fin or n > MAX_CONTROL):
        raise WsError(1002, "control")
    at = 2
    if n in (126, 127):
        width = 2 if n == 126 else 8
        if len(buf) < 2 + width:
            return None
        n, at = int.from_bytes(buf[2 : 2 + width], "big"), 2 + width
        if n <= (125 if width == 2 else 0xFFFF) or n >= 1 << 63:
            raise WsError(1002, "length")
    if n > max_payload:
        raise WsError(1009, "too_big")
    end = at + (4 if masked else 0) + n
    if len(buf) < end:
        return None
    payload = bytes(buf[end - n : end])
    if masked:
        payload = _xor(payload, bytes(buf[at : at + 4]))
    if op == CLOSE and payload:
        code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 0
        if code not in _CLOSE_OK and not 3000 <= code <= 4999:
            raise WsError(1002, "close_code")
        try:
            payload[2:].decode("utf-8")
        except UnicodeDecodeError:
            raise WsError(1007, "close_reason") from None
    return Frame(fin, op, payload), end


MAX_CONTROL_PER_READ = 64  # ping / pong frames accepted from one read (a flood is closed, not answered)
MAX_CONTROL_PER_S = 100  # ... and per second over a link


class Receiver:
    """One direction as a byte stream: binary and continuation payloads in order (boundaries mean nothing); a
    continuation with no open message or a binary frame inside one is 1002; a ping is answered with a pong of the same
    payload (only the most recent ping of one read is answered), a pong ignored; a close ends the stream (``closed`` =
    its code or ``None``) and is answered with 1000. A ping alone yields no stream bytes: a reader never wakes for it.

    Bounded work: frames are decoded in place with a read cursor over a memoryview (the buffer is compacted once per
    ``feed``, never per frame, so a flood of tiny frames is linear); more than :data:`MAX_CONTROL_PER_READ` control
    frames in one read, or :data:`MAX_CONTROL_PER_S` in a second, is ``WsError(1008, "flood")``."""

    def __init__(
        self, sender: str, *, max_payload: int = MAX_FRAME, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._sender, self._max, self._clock = sender, max_payload, clock
        self._buf = bytearray()
        self._open = False
        self.ended = False
        self.closed: int | None = None
        self._win_start, self._win_n = clock(), 0

    def _control_seen(self, n_read: int) -> None:
        now = self._clock()
        if now - self._win_start >= 1.0:
            self._win_start, self._win_n = now, 0
        self._win_n += 1
        if n_read > MAX_CONTROL_PER_READ or self._win_n > MAX_CONTROL_PER_S:
            raise WsError(1008, "flood")

    def feed(self, data: bytes) -> tuple[bytes, list[tuple[int, bytes]]]:
        if self.ended:
            return b"", []
        self._buf += data
        out: list[bytes] = []
        answers: list[tuple[int, bytes]] = []
        last_pong: tuple[int, bytes] | None = None
        controls = pos = 0
        view = memoryview(self._buf)
        try:
            while not self.ended:
                got = decode_frame(view[pos:], sender=self._sender, max_payload=self._max)
                if got is None:
                    break
                f, used = got
                pos += used
                if f.opcode in (CONT, BINARY):
                    if (f.opcode == CONT) is not self._open:
                        raise WsError(1002, "continuation" if f.opcode == CONT else "interleaved")
                    out.append(f.payload)
                    self._open = not f.fin
                elif f.opcode == PING:
                    controls += 1
                    self._control_seen(controls)
                    last_pong = (PONG, f.payload)
                elif f.opcode == PONG:
                    controls += 1
                    self._control_seen(controls)
                elif f.opcode == CLOSE:
                    self.ended = True
                    self.closed = int.from_bytes(f.payload[:2], "big") if f.payload else None
                    if last_pong is not None:
                        answers.append(last_pong)
                        last_pong = None
                    answers.append((CLOSE, (1000).to_bytes(2, "big")))
        finally:
            view.release()
            del self._buf[:pos]
            if self.ended:
                self._buf.clear()
        if last_pong is not None:
            answers.append(last_pong)
        return b"".join(out), answers


def new_key() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + _GUID).digest()).decode("ascii")  # noqa: S324


def build_request(host_header: str, key: str) -> bytes:
    lines = [
        f"GET {LINK_PATH} HTTP/1.1", f"Host: {host_header}", "Upgrade: websocket", "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", f"Sec-WebSocket-Protocol: {SUBPROTOCOL}",
    ]  # fmt: skip
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_SINGLE = {"upgrade", "connection", "sec-websocket-accept", "sec-websocket-protocol", "sec-websocket-extensions"}


def check_response(raw: bytes, key: str) -> None:
    """The answer head (through CRLF CRLF) to a request sent with ``key``; raises :class:`HandshakeFailed`:
    ``too_large``, ``malformed``, ``redirect``, ``status``, ``upgrade``, ``accept``, ``extensions``,
    ``subprotocol``."""
    if len(raw) > MAX_HEAD:
        raise HandshakeFailed("too_large")
    if not raw.isascii() or not raw.endswith(b"\r\n\r\n"):
        raise HandshakeFailed("malformed")
    lines = raw[:-4].decode("ascii").split("\r\n")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        low = name.lower()
        if not colon or not _NAME.fullmatch(name) or "\r" in line or "\n" in line or (low in _SINGLE and low in fields):
            raise HandshakeFailed("malformed")
        fields[low] = value.strip(" \t")
    m = re.fullmatch(r"HTTP/1\.1 ([0-9]{3})(?: .*)?", lines[0])
    if m is None or "\r" in lines[0] or "\n" in lines[0]:
        raise HandshakeFailed("malformed")
    status = int(m[1])
    if status // 100 == 3:
        raise HandshakeFailed("redirect")
    if status != 101:
        raise HandshakeFailed("status")
    conn = {t.strip(" \t").lower() for t in fields.get("connection", "").split(",")}
    if fields.get("upgrade", "").lower() != "websocket" or "upgrade" not in conn:
        raise HandshakeFailed("upgrade")
    if fields.get("sec-websocket-accept") != accept_key(key):
        raise HandshakeFailed("accept")
    if "sec-websocket-extensions" in fields:
        raise HandshakeFailed("extensions")
    if fields.get("sec-websocket-protocol") != SUBPROTOCOL:
        raise HandshakeFailed("subprotocol")
