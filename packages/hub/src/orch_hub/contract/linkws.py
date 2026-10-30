"""Link transports, contract R6 V0 (protocol.md §10.10; spec ``orch-v2-hub-remote.md`` §8, D92; #1594).

The reference the vectors ``contracts/hub/vectors_link_ws.json`` are built from and replayed against on the hub side.
Pure: no I/O, no clock, no state beyond one receiver's buffer. orch-core has its own implementation written from the
protocol text (``orch.hubclient.linkaddr``, ``orch.hubclient.tlslink.wsframe``) and replays the same vectors.

What is here:

- the link address grammar (:func:`parse_link_address`): ``host:port`` (direct) or ``ws://host[:port]`` /
  ``wss://host[:port]``, used by ``hub_where`` (§10.9) and the connect link's endpoint;
- the WebSocket profile of the link: the RFC 6455 frame codec subset (:func:`encode_frame`, :func:`decode_frame`,
  :class:`Receiver`), the upgrade request and response (:func:`build_request`, :func:`check_request`,
  :func:`build_response`, :func:`build_refusal`, :func:`check_response`) and :func:`accept_key`.

The WebSocket only carries the bytes of the link's own TLS 1.3 (pinned, exporter-bound, ALPN unchanged): nothing here
is a security layer, so every rule is "refuse anything unexpected", never "repair".
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
from dataclasses import dataclass

from .hub import Refused

LINK_PATH = "/orch/link/1"
SUBPROTOCOL = "orch-link.1"
WS_VERSION = "13"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_ADDRESS = 300
DEFAULT_PORT = {"ws": 80, "wss": 443}
MAX_FRAME = 256 * 1024  # largest payload a receiver accepts in one frame (decided from the header alone)
MAX_SEND_CHUNK = 64 * 1024  # largest payload a sender puts in one data frame
MAX_CONTROL = 125
MAX_HEAD = 8192  # an upgrade request or response head, CRLF CRLF included
PING_AFTER_S = 25  # the client pings after this long without sending anything
IDLE_CLOSE_S = 75  # either side closes after this long without receiving a single byte
CLOSE_WAIT_S = 1  # after sending a close, wait at most this long for the peer's close

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_KNOWN_OPS = {OP_CONT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG}
_CLOSE_CODES = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}) | frozenset(
    range(3000, 5000)
)


# --- addresses --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkAddress:
    """One parsed link address. ``scheme`` is ``tcp`` (direct ``host:port``), ``ws`` or ``wss``; ``host`` has no
    brackets; ``port`` is the explicit port or the scheme's default."""

    scheme: str
    host: str
    port: int
    text: str

    @property
    def is_ip(self) -> bool:
        return _is_ip(self.host)

    @property
    def sni(self) -> str | None:
        """The inner TLS server name: the DNS name in lower case, never an IP literal (then no SNI)."""
        return None if self.is_ip else self.host.lower()

    @property
    def host_header(self) -> str | None:
        """``Host`` of the upgrade request (ws/wss only): the host as written (``[v6]`` bracketed), ``:port`` only when
        it is not the scheme's default."""
        if self.scheme == "tcp":
            return None
        h = f"[{self.host}]" if ":" in self.host else self.host
        return h if self.port == DEFAULT_PORT[self.scheme] else f"{h}:{self.port}"

    def key(self) -> tuple[str, str, int]:
        """Two addresses are the same when scheme, lower-case host and port are."""
        return (self.scheme, self.host.lower(), self.port)


_REST = re.compile(r"(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(?::([1-9][0-9]{0,4}))?")
_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_DIGITS = re.compile(r"[0-9]+")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _check_host(h: str) -> str:
    if h.startswith("["):
        inner = h[1:-1]
        try:
            ipaddress.IPv6Address(inner)
        except ValueError:
            raise Refused("malformed") from None
        return inner
    labels = h.split(".")
    if not all(_LABEL.fullmatch(lb) for lb in labels):
        raise Refused("malformed")
    if _DIGITS.fullmatch(labels[-1]):  # a numeric last label: only a canonical dotted-quad IPv4 address
        try:
            if str(ipaddress.IPv4Address(h)) != h:
                raise ValueError
        except ValueError:
            raise Refused("malformed") from None
    return h


def parse_link_address(text: object) -> LinkAddress:
    """``host:port`` | ``ws://host[:port]`` | ``wss://host[:port]``; at most 300 ASCII characters; host a DNS name
    (labels of 1-63 letters, digits, inner ``-``; an internationalised name only in its ``xn--`` form), a canonical
    IPv4 address or ``[v6]``; port 1..65535 without a leading zero (required for direct). No path (not even ``/``),
    query, fragment, user info, percent-escape or white space; scheme in lower case. Anything else is ``malformed``."""
    if type(text) is not str or not 1 <= len(text) <= MAX_ADDRESS or not text.isascii():
        raise Refused("malformed")
    scheme, rest = "tcp", text
    for s in ("ws", "wss"):
        if text.startswith(s + "://"):
            scheme, rest = s, text[len(s) + 3 :]
    m = _REST.fullmatch(rest)
    if m is None:
        raise Refused("malformed")
    host = _check_host(m.group(1))
    if m.group(2) is None:
        if scheme == "tcp":
            raise Refused("malformed")
        port = DEFAULT_PORT[scheme]
    else:
        port = int(m.group(2))
        if port > 65535:
            raise Refused("malformed")
    return LinkAddress(scheme, host, port, text)


def check_connect_endpoint(text: object) -> str:
    """The connect link's endpoint (everything after the code's dot, before ``~``): ``local`` or a link address."""
    if text == "local":
        return "local"
    return parse_link_address(text).text


# --- frames -----------------------------------------------------------------------------------------------------


class WsError(Exception):
    """A frame the receiver refuses: it sends a close with ``close`` and drops the link."""

    def __init__(self, close: int, reason: str) -> None:
        super().__init__(f"{close} {reason}")
        self.close = close
        self.reason = reason


@dataclass(frozen=True)
class Frame:
    fin: bool
    opcode: int
    payload: bytes  # unmasked


def _mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def encode_frame(opcode: int, payload: bytes, *, mask: bytes | None = None, fin: bool = True) -> bytes:
    """One frame with the shortest length form. A client passes a fresh random 4-byte ``mask``; a server never masks.
    Senders only use binary (FIN=1, payload ≤ 64 KiB), close, ping and pong (FIN=1, payload ≤ 125)."""
    if opcode not in _KNOWN_OPS or (mask is not None and len(mask) != 4):
        raise ValueError("bad frame")
    if opcode >= OP_CLOSE and (not fin or len(payload) > MAX_CONTROL):
        raise ValueError("bad control frame")
    if len(payload) > MAX_FRAME:
        raise ValueError("frame too big")
    n = len(payload)
    mbit = 0x80 if mask is not None else 0
    if n < 126:
        head = bytes([(0x80 if fin else 0) | opcode, mbit | n])
    elif n < 0x10000:
        head = bytes([(0x80 if fin else 0) | opcode, mbit | 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([(0x80 if fin else 0) | opcode, mbit | 127]) + n.to_bytes(8, "big")
    if mask is None:
        return head + payload
    return head + mask + _mask(payload, mask)


def close_payload(code: int) -> bytes:
    return code.to_bytes(2, "big")


def decode_frame(buf: bytes | memoryview, *, sender: str, max_payload: int = MAX_FRAME) -> tuple[Frame, int] | None:
    """Read one frame from the start of ``buf`` sent by ``sender`` (``client`` or ``server``). ``None``: more bytes
    are needed. Every refusal is decided as early as the bytes allow; an oversize length is refused from the header
    alone, before any payload is read. Checks in this order: RSV bits (1002), text frame (1003), unknown opcode
    (1002), mask bit (client frames masked, server frames not; 1002), control frame fragmented or longer than 125
    (1002), length not in its shortest form or with the top bit set (1002), payload over ``max_payload`` (1009), then
    the close payload: 1 byte or a code outside the allowed set (1002), a reason that is not UTF-8 (1007)."""
    if sender not in ("client", "server"):
        raise ValueError("sender")
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin, opcode, masked, n7 = bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F
    if b0 & 0x70:
        raise WsError(1002, "rsv")
    if opcode == OP_TEXT:
        raise WsError(1003, "text")
    if opcode not in _KNOWN_OPS:
        raise WsError(1002, "opcode")
    if masked != (sender == "client"):
        raise WsError(1002, "mask")
    if opcode >= OP_CLOSE and (not fin or n7 > MAX_CONTROL):
        raise WsError(1002, "control")
    pos = 2
    if n7 == 126:
        if len(buf) < 4:
            return None
        n, pos = int.from_bytes(buf[2:4], "big"), 4
        if n < 126:
            raise WsError(1002, "length")
    elif n7 == 127:
        if len(buf) < 10:
            return None
        n, pos = int.from_bytes(buf[2:10], "big"), 10
        if n < 0x10000 or n >> 63:
            raise WsError(1002, "length")
    else:
        n = n7
    if n > max_payload:
        raise WsError(1009, "too_big")
    key = b""
    if masked:
        if len(buf) < pos + 4:
            return None
        key, pos = bytes(buf[pos : pos + 4]), pos + 4
    if len(buf) < pos + n:
        return None
    payload = bytes(buf[pos : pos + n])
    if masked:
        payload = _mask(payload, key)
    if opcode == OP_CLOSE and payload:
        if len(payload) == 1 or int.from_bytes(payload[:2], "big") not in _CLOSE_CODES:
            raise WsError(1002, "close_code")
        try:
            payload[2:].decode("utf-8")
        except UnicodeDecodeError:
            raise WsError(1007, "close_reason") from None
    return Frame(fin, opcode, payload), pos + n


class Receiver:
    """One direction of a WebSocket as the link reads it: a byte stream. Binary and continuation payloads are appended
    to the stream in order (frame boundaries carry no meaning); a continuation outside a fragmented message or a new
    binary frame inside one is 1002. Ping is answered with a pong carrying the same payload; a pong is only liveness.
    A close ends the stream (``closed`` is its code, ``None`` for an empty close); bytes after it are ignored."""

    def __init__(self, sender: str, *, max_payload: int = MAX_FRAME) -> None:
        self.sender = sender
        self.max_payload = max_payload
        self._buf = bytearray()
        self._in_message = False
        self.ended = False
        self.closed: int | None = None

    def feed(self, data: bytes) -> tuple[bytes, list[tuple[int, bytes]]]:
        """Return ``(stream bytes, answers)``; ``answers`` are ``(opcode, payload)`` frames to send back (a pong per
        ping, one close 1000 when the peer closed). Raises :class:`WsError` for a refused frame."""
        out, answers = bytearray(), []
        if self.ended:
            return b"", []
        self._buf += data
        view, pos = memoryview(self._buf), 0  # a read cursor: one trim per feed, never a copy of the buffer per frame
        try:
            while not self.ended:
                got = decode_frame(view[pos:], sender=self.sender, max_payload=self.max_payload)
                if got is None:
                    break
                f, used = got
                pos += used
                self._step(f, out, answers)
        finally:
            view.release()
            del self._buf[:pos]
            if self.ended:
                self._buf.clear()
        return bytes(out), answers

    def _step(self, f: Frame, out: bytearray, answers: list[tuple[int, bytes]]) -> None:
        if f.opcode == OP_CONT:
            if not self._in_message:
                raise WsError(1002, "continuation")
            out += f.payload
            self._in_message = not f.fin
        elif f.opcode == OP_BINARY:
            if self._in_message:
                raise WsError(1002, "interleaved")
            out += f.payload
            self._in_message = not f.fin
        elif f.opcode == OP_PING:
            answers.append((OP_PONG, f.payload))
        elif f.opcode == OP_CLOSE:
            self.ended = True
            self.closed = int.from_bytes(f.payload[:2], "big") if f.payload else None
            answers.append((OP_CLOSE, close_payload(1000)))


# --- the upgrade ------------------------------------------------------------------------------------------------


class HandshakeRefused(Exception):
    """The hub refuses an upgrade request: it answers :func:`build_refusal` of ``status`` and closes."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status} {reason}")
        self.status = status
        self.reason = reason


class HandshakeFailed(Exception):
    """The client refuses the hub's (or a front's) answer: that address failed; nothing is retried or followed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CHECKED = frozenset(
    {"host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol",
     "sec-websocket-extensions", "sec-websocket-accept", "origin", "sec-websocket-origin", "content-length",
     "transfer-encoding"}
)  # fmt: skip


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")


def build_request(host_header: str, key: str) -> bytes:
    """The client's upgrade request, byte for byte: no Origin, cookie, authorization, user agent or extension."""
    return (
        f"GET {LINK_PATH} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: {WS_VERSION}\r\nSec-WebSocket-Protocol: {SUBPROTOCOL}\r\n\r\n"
    ).encode("ascii")


def build_response(key: str) -> bytes:
    return (
        f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\nSec-WebSocket-Protocol: {SUBPROTOCOL}\r\n\r\n"
    ).encode("ascii")


_REFUSALS = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 426: "Upgrade Required"}


def build_refusal(status: int) -> bytes:
    """The hub's answer to a refused upgrade; the connection is closed after it. No body, no detail."""
    extra = f"Sec-WebSocket-Version: {WS_VERSION}\r\n" if status == 426 else ""
    return f"HTTP/1.1 {status} {_REFUSALS[status]}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode()


def _parse_head(raw: bytes) -> tuple[str, dict[str, str]] | None:
    """``(start line, {lower-case name: value})`` for checked names (duplicates refused) or ``None`` when the head is
    not well formed: ASCII only, CRLF line ends, ends in exactly one empty line, ``name: value`` lines with a token
    name, no folded line."""
    if not raw.endswith(b"\r\n\r\n") or not raw.isascii():
        return None
    lines = raw[:-4].decode("ascii").split("\r\n")
    if any("\r" in ln or "\n" in ln for ln in lines):
        return None
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        name, sep, value = ln.partition(":")
        if not sep or not _TOKEN.fullmatch(name):
            return None
        lname = name.lower()
        if lname in _CHECKED and lname in headers:
            return None
        headers[lname] = value.strip(" \t")
    return lines[0], headers


def _has_token(value: str | None, token: str) -> bool:
    return value is not None and token in {t.strip(" \t").lower() for t in value.split(",")}


def _key_ok(key: str | None) -> bool:
    if key is None or len(key) != 24:
        return False
    try:
        raw = base64.b64decode(key, validate=True)
    except binascii.Error, ValueError:
        return False
    return len(raw) == 16 and base64.b64encode(raw).decode() == key


def check_request(raw: bytes) -> str:
    """The hub's check of an upgrade request head (CRLF CRLF included); returns the ``Sec-WebSocket-Key``. In this
    order: head over 8 KiB or not well formed → 400; anything but ``GET /orch/link/1 HTTP/1.1`` → 404 (``HTTP/1.0`` on
    the right path → 400); an ``Origin`` (or ``Sec-WebSocket-Origin``) header, any value → 403; no ``Host`` → 400;
    ``Upgrade`` not ``websocket`` or ``Connection`` without the ``upgrade`` token → 400; version not ``13`` → 426; a key
    that is not canonical base64 of 16 bytes → 400; any ``Sec-WebSocket-Extensions`` → 400; ``Sec-WebSocket-Protocol``
    not exactly ``orch-link.1`` → 400; a body (``Transfer-Encoding``, or ``Content-Length`` other than ``0``) → 400.
    Every other header (``X-Forwarded-For``, cookies ...) is ignored and never used."""
    if len(raw) > MAX_HEAD:
        raise HandshakeRefused(400, "too_large")
    if not raw.endswith(b"\r\n\r\n") or not raw.isascii():
        raise HandshakeRefused(400, "malformed")
    first = raw.split(b"\r\n", 1)[0].decode("ascii")
    parts = first.split(" ")
    if len(parts) != 3 or parts[0] != "GET" or parts[1] != LINK_PATH:
        raise HandshakeRefused(404, "not_found")
    if parts[2] != "HTTP/1.1":
        raise HandshakeRefused(400, "version")
    if re.search(rb"\r\n(origin|sec-websocket-origin)[ \t]*:", raw, re.IGNORECASE):
        raise HandshakeRefused(403, "origin")
    parsed = _parse_head(raw)
    if parsed is None:
        raise HandshakeRefused(400, "malformed")
    h = parsed[1]
    if not h.get("host"):
        raise HandshakeRefused(400, "host")
    if (h.get("upgrade") or "").lower() != "websocket" or not _has_token(h.get("connection"), "upgrade"):
        raise HandshakeRefused(400, "upgrade")
    if h.get("sec-websocket-version") != WS_VERSION:
        raise HandshakeRefused(426, "version")
    if not _key_ok(h.get("sec-websocket-key")):
        raise HandshakeRefused(400, "key")
    if "sec-websocket-extensions" in h:
        raise HandshakeRefused(400, "extensions")
    if h.get("sec-websocket-protocol") != SUBPROTOCOL:
        raise HandshakeRefused(400, "subprotocol")
    if "transfer-encoding" in h or h.get("content-length", "0") != "0":
        raise HandshakeRefused(400, "body")
    return h["sec-websocket-key"]


def check_response(raw: bytes, key: str) -> None:
    """The client's check of the answer head (CRLF CRLF included) to its request with ``key``. In this order: over
    8 KiB → ``too_large``; not well formed or not ``HTTP/1.1`` → ``malformed``; a 3xx → ``redirect`` (never followed);
    any status but 101 → ``status``; ``Upgrade``/``Connection`` wrong → ``upgrade``; ``Sec-WebSocket-Accept`` missing
    or wrong → ``accept``; any ``Sec-WebSocket-Extensions`` → ``extensions``; ``Sec-WebSocket-Protocol`` not exactly
    ``orch-link.1`` → ``subprotocol``."""
    if len(raw) > MAX_HEAD:
        raise HandshakeFailed("too_large")
    parsed = _parse_head(raw)
    if parsed is None:
        raise HandshakeFailed("malformed")
    first, h = parsed
    m = re.fullmatch(r"HTTP/1\.1 ([0-9]{3})( .*)?", first)
    if m is None:
        raise HandshakeFailed("malformed")
    status = int(m.group(1))
    if 300 <= status < 400:
        raise HandshakeFailed("redirect")
    if status != 101:
        raise HandshakeFailed("status")
    if (h.get("upgrade") or "").lower() != "websocket" or not _has_token(h.get("connection"), "upgrade"):
        raise HandshakeFailed("upgrade")
    if h.get("sec-websocket-accept") != accept_key(key):
        raise HandshakeFailed("accept")
    if "sec-websocket-extensions" in h:
        raise HandshakeFailed("extensions")
    if h.get("sec-websocket-protocol") != SUBPROTOCOL:
        raise HandshakeFailed("subprotocol")
