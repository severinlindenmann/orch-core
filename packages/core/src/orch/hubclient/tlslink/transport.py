"""Where the bytes of the hub link travel (``contracts/hub/protocol.md`` 10.10, R6 #1594; design ``r6-transports``).

The link's TLS 1.3 (card SPKI pin, R1 exporter binding, ALPN) is the only security; this module only decides how its
bytes reach the hub, and never touches the pin:

* **direct**: one TCP connection (today).
* **ws / wss**: [outer TLS for ``wss``: system trust, the name checked] + an RFC 6455 upgrade (``wsframe``) + binary
  frames; the link's TLS runs inside them.
* **HTTP CONNECT** through the proxy of the host's own configuration (:mod:`orch.hubclient.netconfig`), then either of
  the above. The environment's proxy variables are never read; the proxy never comes from the hub or the card; there is
  no fallback to a direct dial.

A *pipe* is a byte stream with deadlines. Layers stack: socket pipe, outer TLS (``ssl`` over memory BIOs), WebSocket.
The caller (:func:`orch.hubclient.tlslink.tls.dial_link`) runs the pinned TLS 1.3 handshake on the result.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
import threading
import time

from orch import fdwait
from orch.wsproto.localdial import dial_host

from ..linkaddr import LinkAddr
from ..netconfig import NetSettings, ProxySettings
from . import wsframe as W
from .errors import LinkClosed, LinkTimeout, ProxyAuth, ProxyRefused, ProxyUnreachable

__all__ = ["Path", "Pipe", "SockPipe", "TlsPipe", "WsPipe", "dial_tcp", "open_path", "proxy_applies"]

CHUNK = 65536
WRITE_S = 10.0
CONTROL_WRITE_S = 3.0


def _now() -> float:
    return time.monotonic()


# --- TCP -------------------------------------------------------------------------------------------------------------


def dial_tcp(host: str, port: int, deadline: float) -> socket.socket:
    """Resolve and connect within the overall deadline (the resolver itself cannot be interrupted, so it runs in a
    helper thread that is abandoned on timeout; a socket it opens late is closed)."""
    out: list[socket.socket | BaseException] = []
    done = threading.Event()
    abandoned = threading.Event()

    def work() -> None:
        try:
            infos = socket.getaddrinfo(dial_host(host), port, type=socket.SOCK_STREAM)  # pin, not name, is the trust
            err: BaseException = OSError("no address")
            for fam, typ, proto, _, addr in infos:
                left = deadline - time.monotonic()
                if left <= 0 or abandoned.is_set():
                    break
                s = socket.socket(fam, typ, proto)
                try:
                    s.settimeout(left)
                    s.connect(addr)
                    if abandoned.is_set():
                        s.close()
                        return
                    out.append(s)
                    return
                except OSError as e:
                    s.close()
                    err = e
            out.append(err)
        except BaseException as e:  # noqa: BLE001
            out.append(e)
        finally:
            done.set()

    threading.Thread(target=work, daemon=True).start()
    if not done.wait(max(deadline - time.monotonic(), 0)):
        abandoned.set()
        raise LinkTimeout
    r = out[0]
    if isinstance(r, socket.socket):
        return r
    raise LinkClosed(type(r).__name__)


# --- pipes -----------------------------------------------------------------------------------------------------------


class Pipe:
    """A byte stream. ``poll(timeout)``: ``None`` when nothing arrived in time, else bytes (possibly empty: only
    control traffic arrived); ``read(deadline)``: non-empty bytes; ``write``: everything or an error. EOF is
    :class:`LinkClosed`, a passed deadline :class:`LinkTimeout`. One thread at a time."""

    def poll(self, timeout: float) -> bytes | None:
        raise NotImplementedError

    def write(self, data: bytes, deadline: float) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def read(self, deadline: float) -> bytes:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LinkTimeout
            got = self.poll(left)
            if got:
                return got


class SockPipe(Pipe):
    def __init__(self, sock: socket.socket) -> None:
        sock.setblocking(False)
        self._s = sock

    def _recv(self) -> bytes | None:
        try:
            data = self._s.recv(CHUNK)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError as e:
            raise LinkClosed(type(e).__name__) from None
        if not data:
            raise LinkClosed("eof")
        return data

    def poll(self, timeout: float) -> bytes | None:
        got = self._recv()
        if got is not None:
            return got
        try:
            if not fdwait.readable(self._s, max(timeout, 0.0)):
                return None
        except (OSError, ValueError):
            raise LinkClosed("closed") from None
        return self._recv()

    def write(self, data: bytes, deadline: float) -> None:
        view, off = memoryview(data), 0
        while off < len(view):
            try:
                off += self._s.send(view[off:])
            except (BlockingIOError, InterruptedError):
                left = deadline - time.monotonic()
                if left <= 0 or not fdwait.writable(self._s, left):
                    raise LinkTimeout from None
            except OSError as e:
                raise LinkClosed(type(e).__name__) from None

    def close(self) -> None:
        try:
            self._s.close()
        except OSError:
            pass


def outer_context() -> ssl.SSLContext:
    """The context of every outer TLS (``wss``, an ``https`` proxy): the system trust store and the name checked. A seam
    for tests; it is not a security layer (the link's own pinned TLS is)."""
    return ssl.create_default_context()


class TlsPipe(Pipe):
    """Outer TLS (stdlib ``ssl``, memory BIOs) over another pipe. Raises :class:`LinkClosed` when the certificate or
    the name is wrong; it is never retried without TLS."""

    def __init__(self, inner: Pipe, server_hostname: str, deadline: float) -> None:
        self._inner = inner
        self._in, self._out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._o = outer_context().wrap_bio(self._in, self._out, server_hostname=server_hostname)
        while True:
            try:
                self._o.do_handshake()
                break
            except ssl.SSLWantReadError:
                self._flush(deadline)
                self._in.write(inner.read(deadline))
            except ssl.SSLWantWriteError:
                self._flush(deadline)
            except (ssl.SSLError, ValueError):
                raise LinkClosed("outer_tls") from None
        self._flush(deadline)

    def _flush(self, deadline: float) -> None:
        data = self._out.read()
        if data:
            self._inner.write(data, deadline)

    def _app(self) -> bytes | None:
        try:
            return self._o.read(CHUNK)
        except ssl.SSLWantReadError:
            return None
        except (ssl.SSLError, ValueError):
            raise LinkClosed("outer_tls") from None

    def poll(self, timeout: float) -> bytes | None:
        got = self._app()
        if got:
            return got
        raw = self._inner.poll(timeout)
        if raw is None:
            return None
        self._in.write(raw)
        got = self._app()
        self._flush(time.monotonic() + WRITE_S)
        return got or b""

    def write(self, data: bytes, deadline: float) -> None:
        mv = memoryview(data)
        for off in range(0, len(mv), 16384):
            try:
                self._o.write(bytes(mv[off : off + 16384]))
            except (ssl.SSLError, ValueError):
                raise LinkClosed("outer_tls") from None
            self._flush(deadline)

    def close(self) -> None:
        self._inner.close()


def _read_head(pipe: Pipe, deadline: float, limit: int) -> tuple[bytes, bytes]:
    """An HTTP head through CRLF CRLF (at most ``limit`` bytes) and whatever followed it in the last chunk."""
    buf = b""
    while True:
        i = buf.find(b"\r\n\r\n")
        if i >= 0:
            if i + 4 > limit:
                raise ValueError("too_large")
            return buf[: i + 4], buf[i + 4 :]
        if len(buf) > limit:
            raise ValueError("too_large")
        buf += pipe.read(deadline)


class WsPipe(Pipe):
    """The WebSocket layer of the profile (``wsframe``): binary frames out (client frames masked, at most 64 KiB), a
    stream in. A ping from the other side is answered and yields no stream bytes (``poll`` returns ``b""``); this side
    pings after 25 s without sending and closes after 75 s without receiving a byte. Those timers run inside
    ``poll`` / ``write``, so a link that is waited on keeps itself alive."""

    def __init__(self, inner: Pipe) -> None:
        self._inner = inner
        self._rx = W.Receiver("server")
        self._ended = False
        self._last_in = self._last_out = _now()
        self._closed = False

    @classmethod
    def connect(cls, inner: Pipe, addr: LinkAddr, deadline: float) -> WsPipe:
        key = W.new_key()
        host = addr.host_header
        assert host is not None
        inner.write(W.build_request(host, key), deadline)
        try:
            head, rest = _read_head(inner, deadline, W.MAX_HEAD)
        except ValueError:
            raise LinkClosed("ws_too_large") from None
        try:
            W.check_response(head, key)
        except W.HandshakeFailed as e:
            raise LinkClosed(f"ws_{e.reason}") from None
        pipe = cls(inner)
        if rest:
            pipe._pending = pipe._ingest(rest)
        return pipe

    _pending = b""

    def _send(self, opcode: int, payload: bytes, seconds: float = CONTROL_WRITE_S) -> None:
        self._inner.write(W.encode_frame(opcode, payload, mask=os.urandom(4)), time.monotonic() + seconds)
        self._last_out = _now()

    def _ingest(self, raw: bytes) -> bytes:
        try:
            data, answers = self._rx.feed(raw)
        except W.WsError as e:
            self._fail(e.code)
            raise LinkClosed(f"ws_{e.reason}") from None
        for op, payload in answers:
            try:
                self._send(op, payload)
            except (LinkClosed, LinkTimeout):
                self._ended = True
        if self._rx.ended:
            self._ended = True
        return data

    def _fail(self, code: int) -> None:
        self._ended = True
        try:
            self._send(W.CLOSE, code.to_bytes(2, "big"), 1.0)
        except (LinkClosed, LinkTimeout):
            pass

    def poll(self, timeout: float) -> bytes | None:
        if self._pending:
            out, self._pending = self._pending, b""
            return out
        end = _now() + max(timeout, 0.0)
        while True:
            if self._ended:
                raise LinkClosed("ws_closed")
            now = _now()
            if now - self._last_in >= W.IDLE_CLOSE_S:
                self.close()
                raise LinkClosed("ws_idle")
            if now - self._last_out >= W.PING_AFTER_S:
                self._send(W.PING, b"")
            left = end - now
            raw = self._inner.poll(min(max(left, 0.0), 1.0))
            if raw is not None:
                self._last_in = _now()
                return self._ingest(raw)
            if left <= 0:
                return None

    def write(self, data: bytes, deadline: float) -> None:
        if self._ended:
            raise LinkClosed("ws_closed")
        mv = memoryview(data)
        for off in range(0, len(mv), W.MAX_SEND_CHUNK):
            self._inner.write(
                W.encode_frame(W.BINARY, bytes(mv[off : off + W.MAX_SEND_CHUNK]), mask=os.urandom(4)), deadline
            )
            self._last_out = _now()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._ended:
            try:
                self._send(W.CLOSE, (1000).to_bytes(2, "big"), 1.0)
            except (LinkClosed, LinkTimeout, OSError):
                pass
        self._inner.close()


# --- HTTP CONNECT ----------------------------------------------------------------------------------------------------

_STATUS = re.compile(r"HTTP/1\.[01] ([0-9]{3})(?: .*)?")
NTLM_TEXT = (
    "This network's proxy signs people in a way orch does not support yet (NTLM or Negotiate). Ask your IT team for "
    "an exception, or for a proxy that accepts a user name and password."
)


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def proxy_applies(addr: LinkAddr, net: NetSettings | None) -> ProxySettings | None:
    """The proxy this address is dialled through, or ``None``: only the host's own settings, never loopback."""
    if net is None or net.proxy is None or is_loopback_host(addr.host):
        return None
    return net.proxy


def _connect_tunnel(pipe: Pipe, addr: LinkAddr, proxy: ProxySettings, deadline: float) -> None:
    host = f"[{addr.host}]" if ":" in addr.host else addr.host
    target = f"{host}:{addr.port}"
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    auth = proxy.authorization
    if auth is not None:
        lines.append(f"Proxy-Authorization: {auth}")  # on the CONNECT only, never inside the tunnel
    try:
        pipe.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"), deadline)
        head, rest = _read_head(pipe, deadline, W.MAX_HEAD)
    except ValueError:
        raise ProxyRefused("The proxy answered with something that is not a normal reply.") from None
    except (LinkClosed, LinkTimeout):
        raise ProxyUnreachable("The proxy did not answer.") from None
    if not head.isascii():
        raise ProxyRefused("The proxy answered with something that is not a normal reply.")
    rows = head[:-4].decode("ascii").split("\r\n")
    m = _STATUS.fullmatch(rows[0])
    if m is None:
        raise ProxyRefused("The proxy answered with something that is not a normal reply.")
    status = int(m[1])
    if status == 407:
        offered: set[str] = set()
        for row in rows[1:]:
            name, _, value = row.partition(":")
            if name.strip().lower() == "proxy-authenticate":
                offered.add(value.strip().split(" ", 1)[0].lower())
        if offered and "basic" not in offered:
            raise ProxyAuth(NTLM_TEXT)
        if auth is None:
            raise ProxyAuth("The proxy asks for a user name and password.")
        raise ProxyAuth("The proxy did not accept the user name and password.")
    if not 200 <= status < 300:
        raise ProxyRefused(f"The proxy refused the connection (status {status}).")
    if rest:
        raise ProxyRefused("The proxy sent data before the connection was open.")


# --- the path --------------------------------------------------------------------------------------------------------


class Path:
    """What :func:`open_path` built. ``pipe`` is ``None`` when the link's TLS can run straight on ``sock`` (a direct
    dial or a tunnel through an ``http`` proxy); otherwise the link's TLS runs over ``pipe``. ``via`` says that a proxy
    or a WebSocket front is in the way (a pin mismatch then means inspection)."""

    def __init__(self, sock: socket.socket, pipe: Pipe | None, via: bool) -> None:
        self.sock, self.pipe, self.via = sock, pipe, via

    def close(self) -> None:
        if self.pipe is not None:
            self.pipe.close()
        try:
            self.sock.close()
        except OSError:
            pass


def open_path(addr: LinkAddr, net: NetSettings | None, deadline: float, *, connect_host: str | None = None) -> Path:
    """Reach ``addr`` as its scheme and the host's proxy settings say, and nothing else: direct TCP, or CONNECT through
    the one configured proxy (the name is sent as text, never resolved here), then a WebSocket upgrade for ``ws`` /
    ``wss``. ``connect_host`` (a checked IP for the name) is used for the direct dial only."""
    proxy = proxy_applies(addr, net)
    sock: socket.socket | None = None
    pipe: Pipe | None = None
    try:
        if proxy is not None:
            try:
                sock = dial_tcp(proxy.host, proxy.port, deadline)
            except (LinkClosed, LinkTimeout):
                raise ProxyUnreachable("The proxy could not be reached.") from None
            pipe = SockPipe(sock)
            if proxy.scheme == "https":
                try:
                    pipe = TlsPipe(pipe, proxy.host, deadline)
                except (LinkClosed, LinkTimeout):
                    raise ProxyUnreachable("The proxy's secure connection could not be set up.") from None
            _connect_tunnel(pipe, addr, proxy, deadline)
            if proxy.scheme == "http":
                pipe = None  # the tunnel is the raw socket
        else:
            sock = dial_tcp(connect_host or addr.host, addr.port, deadline)
        if addr.scheme != "tcp":
            base = pipe or SockPipe(sock)
            if addr.scheme == "wss":
                base = TlsPipe(base, addr.host, deadline)
            pipe = WsPipe.connect(base, addr, deadline)
        return Path(sock, pipe, via=proxy is not None or addr.scheme != "tcp")
    except BaseException:
        if pipe is not None:
            pipe.close()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        raise
