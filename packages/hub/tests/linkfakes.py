"""R6 T-B fakes (#1594): a reference WebSocket client for the hub's link accept. It speaks the contract's codec
(``orch_hub.contract.linkws``) and runs the link's inner TLS 1.3 on a memory BIO through the same ``BioTlsConn``
the hub uses (client side), so a test drives a real upgrade, a real pinned handshake and the real link frames."""

from __future__ import annotations

import base64
import os
import select
import socket
import time

from OpenSSL import SSL

from orch_hub.contract import linkws as W
from orch_hub.link import tls
from orch_hub.link.tls import LinkClosed, LinkTimeout, PinMismatch


class ClientPipe:
    """Client side of the WebSocket: masks every frame, reads unmasked server frames, answers pings."""

    def __init__(self, sock: socket.socket, leftover: bytes = b"") -> None:
        self.sock = sock
        self.rx = W.Receiver("server")
        self.pending = leftover
        self.pings = 0
        self.closed_by_peer: int | None = None
        self.sock.setblocking(False)

    def _send(self, data: bytes, deadline: float) -> None:
        view, off = memoryview(data), 0
        while off < len(view):
            if time.monotonic() > deadline:
                raise LinkTimeout
            try:
                off += self.sock.send(view[off:])
            except BlockingIOError:
                select.select([], [self.sock], [], 0.1)
            except OSError:
                raise LinkClosed("write") from None

    def frame(self, opcode: int, payload: bytes, deadline: float | None = None) -> None:
        self._send(W.encode_frame(opcode, payload, mask=os.urandom(4)), deadline or time.monotonic() + 5)

    def write_stream(self, data: bytes, deadline: float) -> None:
        for i in range(0, len(data), W.MAX_SEND_CHUNK):
            self.frame(W.OP_BINARY, data[i : i + W.MAX_SEND_CHUNK], deadline)

    def read_stream(self, timeout: float) -> bytes:
        if self.rx.ended:
            raise LinkClosed("ws closed")
        if self.pending:
            data, self.pending = self.pending, b""
        else:
            r, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
            if not r:
                return b""
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                return b""
            except OSError:
                raise LinkClosed("read") from None
            if not data:
                raise LinkClosed("eof")
        try:
            out, answers = self.rx.feed(data)
        except W.WsError:
            raise LinkClosed("ws") from None
        for op, payload in answers:
            if op == W.OP_PONG:
                self.frame(W.OP_PONG, payload)
            elif op == W.OP_CLOSE:
                self.closed_by_peer = 1000
        if self.rx.ended and not out:
            raise LinkClosed("ws closed")
        return out

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def read_head(sock: socket.socket, timeout: float = 5.0) -> tuple[bytes, bytes]:
    """The HTTP head the hub answers with, and any bytes that followed it."""
    end = time.monotonic() + timeout
    buf = b""
    sock.settimeout(0.5)
    while b"\r\n\r\n" not in buf:
        if time.monotonic() > end:
            raise TimeoutError("no head")
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
    i = buf.find(b"\r\n\r\n") + 4
    return buf[:i], buf[i:]


def status_of(head: bytes) -> int | None:
    parts = head.split(b" ", 2)
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


def new_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def upgrade_request(host_header: str, key: str, extra: str = "") -> bytes:
    """The reference request, with ``extra`` header lines (``"X-Forwarded-For: 1.2.3.4\\r\\n"``) added before the end."""
    raw = W.build_request(host_header, key)
    return raw[:-2] + extra.encode() + b"\r\n"


def ws_dial(
    addr: tuple[str, int],
    pin: str,
    *,
    alpn: tuple[bytes, ...] = (),
    extra: str = "",
    timeout: float = 5.0,
) -> tls.BioTlsConn:
    """Upgrade at ``addr``, then the link's TLS 1.3 over the frames, pinned like a host: the SPKI is compared right after
    the handshake and before a byte is sent."""
    sock = socket.create_connection(addr, timeout=timeout)
    key = new_key()
    sock.sendall(upgrade_request(f"{addr[0]}:{addr[1]}", key, extra))
    head, rest = read_head(sock, timeout)
    W.check_response(head, key)
    pipe = ClientPipe(sock, rest)
    ctx = tls._context(SSL.TLS_CLIENT_METHOD)
    if alpn:
        ctx.set_alpn_protos(list(alpn))
    c = SSL.Connection(ctx, None)
    c.set_connect_state()
    conn = tls.BioTlsConn(c, pipe)
    conn.pipe = pipe  # type: ignore[attr-defined]  # the tests look at pings and closes
    try:
        conn.handshake(time.monotonic() + timeout)
        if conn.peer_spki_sha256() != pin:
            raise PinMismatch
    except BaseException:
        conn.close()
        raise
    return conn
