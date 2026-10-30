"""The hub's WebSocket accept for the link (R6 #1594, contracts/hub/protocol.md 10.10.7).

A separate listener (``--link-ws-listen``) serves ONLY ``GET /orch/link/1``: the upgrade of 10.10.3 (checked by the
contract reference :func:`orch_hub.contract.linkws.check_request`, so the vectors and the live code are one function),
then the link's own TLS 1.3 runs over the binary frames (:class:`orch_hub.link.tls.BioTlsConn` on a :class:`WsPipe`).
The WebSocket carries bytes and nothing else: no header, address or frame boundary is ever believed or used for a
decision (the inner TLS pin, the R1 exporter and the ALPN are the security).

Plain HTTP/1.1 on the hub side: an outer TLS, if there is one, belongs to the front in front of this listener.
"""

from __future__ import annotations

import select
import socket
import threading
import time

from OpenSSL import SSL

from ..contract import linkws as W
from . import tls
from .tls import LinkClosed, LinkTimeout

HEAD_TIMEOUT = 5.0  # the upgrade head must arrive whole within this long (10.10.3)
#: Pre-auth limits (R6 review): what one unauthenticated peer may make the hub do before the inner TLS handshake is over.
PRE_BUDGET_BYTES = 64 * 1024  # bytes received before the handshake completed (a ClientHello is ~1 KiB)
MAX_CONTROL_PER_FEED = 16  # control frames answered from one read; more is a flood
MAX_CONTROL_PER_SECOND = 50  # a legitimate client pings every 25 s


def _refuse(sock: socket.socket, status: int) -> None:
    try:
        sock.settimeout(1.0)
        sock.sendall(W.build_refusal(status))
    except OSError:
        pass


def upgrade(sock: socket.socket, *, head_timeout: float = HEAD_TIMEOUT, stop: threading.Event | None = None) -> bytes:
    """Read and check the upgrade request, answer 101 (or the refusal of 10.10.3 and raise :class:`LinkClosed`).
    Returns the bytes that followed the head (the first frames, normally none). Nothing but the head is interpreted
    here; the socket is left non-blocking."""
    end = time.monotonic() + head_timeout
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        if len(buf) > W.MAX_HEAD:
            _refuse(sock, 400)
            raise LinkClosed("head too large")
        left = end - time.monotonic()
        if left <= 0 or (stop is not None and stop.is_set()):
            raise LinkClosed("head timeout")  # silence: a slow-loris gets nothing, and its slot back
        try:
            sock.settimeout(min(left, 0.5))
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            raise LinkClosed("head read") from None
        if not chunk:
            raise LinkClosed("eof")
        buf += chunk
    cut = bytes(buf).index(b"\r\n\r\n") + 4
    head, rest = bytes(buf[:cut]), bytes(buf[cut:])
    try:
        key = W.check_request(head)
    except W.HandshakeRefused as e:
        _refuse(sock, e.status)
        raise LinkClosed(f"upgrade refused {e.status} {e.reason}") from None
    try:
        sock.settimeout(2.0)
        sock.sendall(W.build_response(key))
    except OSError:
        raise LinkClosed("upgrade write") from None
    sock.setblocking(False)
    return rest


class WsPipe:
    """The bytes of one WebSocket (server side: frames in are masked, frames out are not). ``read_stream`` returns the
    stream bytes of the frames that arrived and answers pings itself; ``write_stream`` cuts bytes into binary frames of
    at most 64 KiB. Outer liveness: a ping after ``ping_after`` seconds without sending anything, a close after
    ``idle_close`` seconds without receiving a byte. Frames are written whole under one lock (a pong never splits a
    data frame)."""

    def __init__(
        self,
        sock: socket.socket,
        leftover: bytes = b"",
        *,
        stop: threading.Event | None = None,
        ping_after: float = W.PING_AFTER_S,
        idle_close: float = W.IDLE_CLOSE_S,
    ) -> None:
        self._s, self._stop = sock, stop
        self._rx = W.Receiver("client")
        self._fw = threading.Lock()
        self._ping_after, self._idle_close = ping_after, idle_close
        self._last_rx = self._last_tx = time.monotonic()
        self._pending = leftover
        self._closed = False
        self._secure = False  # the inner TLS handshake is over: the pre-auth byte budget no longer applies
        self._rx_total = 0
        self._control: list[float] = []  # arrival times of ping answers in the last second
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    # --- writing ----------------------------------------------------------------------------------------------

    def _send_raw(self, data: bytes, deadline: float) -> None:
        view, off = memoryview(data), 0
        while off < len(view):
            if self._stop is not None and self._stop.is_set():
                raise LinkClosed("stopping")
            left = deadline - time.monotonic()
            if left <= 0:
                raise LinkTimeout
            try:
                off += self._s.send(view[off:])
            except BlockingIOError, InterruptedError:
                try:
                    select.select([], [self._s], [], min(left, 0.2))
                except OSError, ValueError:
                    raise LinkClosed("write") from None
            except OSError, ValueError:
                raise LinkClosed("write") from None
        self._last_tx = time.monotonic()

    def _frame(self, opcode: int, payload: bytes, deadline: float) -> None:
        data = W.encode_frame(opcode, payload)
        with self._fw:
            self._send_raw(data, deadline)

    def write_stream(self, data: bytes, deadline: float) -> None:
        for i in range(0, len(data), W.MAX_SEND_CHUNK):
            self._frame(W.OP_BINARY, data[i : i + W.MAX_SEND_CHUNK], deadline)

    def ping(self) -> None:
        self._frame(W.OP_PING, b"", time.monotonic() + 5.0)

    # --- reading ----------------------------------------------------------------------------------------------

    def mark_secure(self) -> None:
        self._secure = True

    def _feed(self, data: bytes) -> bytes:
        self._rx_total += len(data)
        if not self._secure and self._rx_total > PRE_BUDGET_BYTES:
            self._fail(1008)
            raise LinkClosed("ws pre-auth budget")
        try:
            out, answers = self._rx.feed(data)
        except W.WsError as e:
            self._fail(e.close)
            raise LinkClosed(f"ws {e.close} {e.reason}") from None
        pongs = [a for a in answers if a[0] == W.OP_PONG]
        now = time.monotonic()
        self._control = [t for t in self._control if now - t < 1.0] + [now] * len(pongs)
        if len(pongs) > MAX_CONTROL_PER_FEED or len(self._control) > MAX_CONTROL_PER_SECOND:
            self._fail(1008)  # a ping flood: no pong at all
            raise LinkClosed("ws control flood")
        # RFC 6455 5.5.3: answering only the most recent ping is enough
        send = ([pongs[-1]] if pongs else []) + [a for a in answers if a[0] != W.OP_PONG]
        for op, payload in send:
            try:
                self._frame(op, payload, time.monotonic() + 5.0)
            except LinkClosed, LinkTimeout:
                break
        return out

    def _fail(self, code: int) -> None:
        try:
            self._frame(W.OP_CLOSE, W.close_payload(code), time.monotonic() + 1.0)
        except LinkClosed, LinkTimeout:
            pass

    def read_stream(self, timeout: float) -> bytes:
        """Stream bytes that arrived within ``timeout`` (b"" when only control frames or nothing came)."""
        if self._rx.ended:
            raise LinkClosed("ws closed")
        if self._pending:
            data, self._pending = self._pending, b""
            return self._feed(data)
        try:
            r, _, _ = select.select([self._s], [], [], max(0.0, timeout))
        except OSError, ValueError:
            raise LinkClosed("read") from None
        now = time.monotonic()
        if not r:
            if now - self._last_rx > self._idle_close:
                self._fail(1001)
                raise LinkClosed("ws idle")
            if now - self._last_tx >= self._ping_after:
                try:
                    self.ping()
                except LinkTimeout:
                    raise LinkClosed("ws ping") from None
            return b""
        try:
            data = self._s.recv(65536)
        except BlockingIOError, InterruptedError:
            return b""
        except OSError, ValueError:
            raise LinkClosed("read") from None
        if not data:
            raise LinkClosed("eof")
        self._last_rx = now
        return self._feed(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fw.acquire(blocking=False):  # never wait: a stuck writer must not block the close
            try:
                self._s.send(W.encode_frame(W.OP_CLOSE, W.close_payload(1000)))
            except OSError, ValueError:
                pass
            finally:
                self._fw.release()
        try:
            self._s.close()
        except OSError:
            pass


def accept_ws(
    ctx: SSL.Context,
    sock: socket.socket,
    deadline: float,
    stop: threading.Event | None = None,
    *,
    head_timeout: float = HEAD_TIMEOUT,
    ping_after: float = W.PING_AFTER_S,
    idle_close: float = W.IDLE_CLOSE_S,
) -> tls.BioTlsConn:
    """Upgrade, then the link's TLS server handshake over the frames. Raises :class:`LinkClosed`/:class:`LinkTimeout`;
    on failure the socket is closed."""
    pipe = None
    try:
        rest = upgrade(sock, head_timeout=head_timeout, stop=stop)
        pipe = WsPipe(sock, rest, stop=stop, ping_after=ping_after, idle_close=idle_close)
        c = SSL.Connection(ctx, None)
        c.set_accept_state()
        t = tls.BioTlsConn(c, pipe, stop)
        t.handshake(deadline)
        pipe.mark_secure()
        return t
    except BaseException:
        if pipe is not None:
            pipe.close()
        else:
            sock.close()
        raise
