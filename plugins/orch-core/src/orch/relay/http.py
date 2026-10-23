"""The stdlib relay transport (contract 11A.7): one :class:`RelayConn` per thread.

* ``http.client`` only. It reads no proxy variable from the environment; a proxy is used only when ``link.json``
  names one (``host:port``), through ``set_tunnel``.
* TLS: ``ssl.create_default_context()`` (``CERT_REQUIRED``, hostname check, TLS >= 1.2), or the ``ca_file`` that
  ``link.json`` names (then only that CA is trusted). Decided by orch-52 (13 Oct): no emptiness probe of the default
  store (``get_ca_certs()`` is empty for capath-only stores that verify fine); a store that trusts nothing fails
  closed at the handshake (kind ``tls``). There is no unverified fallback and no knob to turn verification off.
  OpenSSL's ``SSL_CERT_FILE``/``SSL_CERT_DIR`` environment variables are honoured by the default context; that is
  acceptable, because controlling the host process's environment already means being the host user.
  Plain ``http`` is only for the loopback origins that :func:`wire.check_origin` allows.
* Every call has a **total** deadline on the injected monotonic clock covering connect, TLS, headers and body: a
  watchdog shuts the socket down when it passes (so a relay that drips header bytes cannot stretch a call), and the
  socket timeout is set to the time remaining before every bounded body read. The watchdog's socket is registered
  before ``connect`` blocks; a deadline or :meth:`RelayConn.shutdown` that passed before that fails the call at once
  (round 6, orch-52 #3).
* The body is read in bounded pieces against ``cap`` whatever ``Content-Length`` or chunked framing says; over the cap
  the connection is closed and the call fails as transient ``too_large``.
* No redirect is followed: any 3xx except 304 is :class:`RedirectRefused` (permanent; alarm ``relay_redirect``).
  A 304 is a cache validation result, not a redirect, and is returned to the caller.
* Connections are kept alive and reopened after any error. :meth:`RelayConn.shutdown` may be called from another
  thread (11A.7 shutdown: ``socket.shutdown(SHUT_RDWR)`` ends a blocked ``recv``).
* Redaction (11A.9): logs carry the route template, status, sizes and timings; errors carry the route and a kind.
  Never a header value, a target with its query, or a body.
"""

from __future__ import annotations

import http.client
import logging
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from . import wire

__all__ = ["CutOver", "RedirectRefused", "RelayConn", "Response", "Transport", "TransportError", "tls_context"]

log = logging.getLogger("orch.relay.http")

READ_PIECE = 64 * 1024
MAX_LOOKUPS = 4  # outstanding name lookups per connection; above it a call fails at once ("resolver stuck")
_HEADER_VALUE = re.compile(r"[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?")
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
_TARGET = re.compile(r"/[A-Za-z0-9._~%/:@!$&'()*+,;=?-]{0,2047}")
_PROXY = re.compile(r"(?P<host>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?):(?P<port>[1-9][0-9]{0,4})")


class TransportError(Exception):
    """A call that produced no relay answer. ``kind``: ``connect``, ``tls``, ``deadline``, ``too_large``,
    ``truncated``, ``protocol``, ``request`` (refused locally), ``shutdown`` (the connection was shut down; use a new
    one) or ``redirect``. Transient unless stated."""

    transient = True

    def __init__(self, kind: str, route: str) -> None:
        super().__init__(f"relay transport {kind} on {route}")
        self.kind, self.route = kind, route


class _ResolverStuck(OSError):
    """``MAX_LOOKUPS`` name lookups of this connection are still outstanding."""


class RedirectRefused(TransportError):
    transient = False

    def __init__(self, route: str) -> None:
        super().__init__("redirect", route)


class CutOver(TransportError):
    """The workspace's direct relay path is closed (V3 C10): the host's gate refused the route before a byte was sent.
    Permanent until the owner reopens the path."""

    transient = False

    def __init__(self, route: str) -> None:
        super().__init__("cut_over", route)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # lower-case names; repeated headers joined with ", "
    body: bytes

    def __repr__(self) -> str:
        return f"Response(status={self.status}, bytes={len(self.body)})"


class Transport(Protocol):
    def request(
        self,
        method: str,
        target: str,
        *,
        route: str,
        headers: Mapping[str, str],
        body: bytes | None,
        deadline_s: float,
        cap: int,
    ) -> Response: ...  # a transport may also take ``resend_ok: bool = True`` (RelayConn does)

    def shutdown(self) -> None: ...


def tls_context(ca_file: str | None, *, factory: Callable[..., ssl.SSLContext] = ssl.create_default_context):
    """A verifying client context: ``ssl.create_default_context()`` (the platform's default paths, capath included), or
    only ``ca_file`` when ``link.json`` names one."""
    ctx = factory(cafile=ca_file) if ca_file else factory()
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    if ctx.minimum_version < ssl.TLSVersion.TLSv1_2:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


IDLE_REUSE_S = 3.0  # a kept connection idle longer is not reused: the relay (uvicorn) closes an idle one after 5 s


class _Stale(Exception):
    """Internal: a reused connection failed before any response byte (the peer had closed it)."""


def _send_reset(e: BaseException) -> bool:
    """A failure while WRITING the request to a kept connection the peer had closed."""
    return isinstance(e, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))


class RelayConn:
    """One keep-alive connection to the relay origin, used by one thread at a time."""

    def __init__(
        self,
        origin: str,
        *,
        ca_file: str | None = None,
        proxy: str | None = None,
        mono: Callable[[], float] = time.monotonic,
        context_factory: Callable[..., ssl.SSLContext] = ssl.create_default_context,
        resolve: Callable[..., list] = socket.getaddrinfo,
        allowed: Callable[[str, str], bool] | None = None,
    ) -> None:
        #: C10: the host's gate for one workspace, ``allowed(method, target) -> bool``. When it says no, the call raises
        #: :class:`CutOver` before any byte is sent. The only way around it is not to hold a RelayConn of the workspace.
        self._allowed = allowed
        try:
            self.scheme, self.host, self.port = wire.origin_parts(origin)
        except wire.WireError:
            raise TransportError("request", "origin") from None
        self.origin = origin
        self.proxy: tuple[str, int] | None = None
        if proxy is not None:
            m = _PROXY.fullmatch(proxy) if type(proxy) is str else None
            if m is None or int(m.group("port")) > 65_535:
                raise TransportError("request", "proxy")
            self.proxy = (m.group("host"), int(m.group("port")))
            if self.scheme != "https":  # the login and the bearer token would cross the proxy in cleartext
                raise TransportError("request", "proxy")
        self._ctx = tls_context(ca_file, factory=context_factory) if self.scheme == "https" else None
        self._mono = mono
        self._conn: http.client.HTTPConnection | None = None
        self._resolve = resolve
        self._deadline = 0.0
        self._wake = threading.Event()  # the current call's: set by its own lookup helper or by shutdown()
        self._shut = threading.Event()  # set by shutdown(), never cleared: a shut connection stays shut
        self._lookups = 0  # outstanding helper threads (guarded by _fd_lock)
        self._lookup_wakeups = 0  # wait() returns in _lookup (tests: no busy spin)
        self._watch: socket.socket | None = None  # our own dup of the connection's socket, for the watchdog
        self._fired = threading.Event()  # the current call's watchdog has fired
        self._fd_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._last_used = 0.0  # when the last response was fully read (the idle clock of the kept connection)

    def __repr__(self) -> str:
        return f"RelayConn({self.origin})"

    # -- sockets ------------------------------------------------------------------------------------------------

    def _lookup(self, host: str, port: int) -> list:
        """``getaddrinfo`` in a helper thread, bounded by the call's total deadline and by :meth:`shutdown` (review D5:
        a stuck resolver is outside both the socket timeout and the watchdog, since no socket exists yet). At most
        ``MAX_LOOKUPS`` helpers are outstanding per connection; each wakes only its own call's event, so a stale
        helper never wakes a later call."""
        box: dict[str, object] = {}
        done = threading.Event()
        wake = self._wake
        with self._fd_lock:
            if self._lookups >= MAX_LOOKUPS:
                raise _ResolverStuck("resolver stuck")
            self._lookups += 1

        def run() -> None:
            try:
                box["r"] = self._resolve(host, port, 0, socket.SOCK_STREAM)
            except OSError as e:
                box["e"] = e
            finally:
                with self._fd_lock:
                    self._lookups -= 1
                done.set()
                wake.set()

        try:
            threading.Thread(target=run, name="orch-relay-dns", daemon=True).start()
        except RuntimeError as e:  # thread limit: the helper never ran, so its slot must not leak (round 4, N4)
            with self._fd_lock:
                self._lookups -= 1
            raise _ResolverStuck("cannot start a name lookup") from e
        while not done.is_set():
            left = self._deadline - self._mono()
            if left <= 0 or self._shut.is_set():
                raise TimeoutError("name resolution")
            wake.wait(left)
            wake.clear()
            self._lookup_wakeups += 1
        if "e" in box:
            raise box["e"]  # type: ignore[misc]
        return box["r"]  # type: ignore[return-value]

    def _socket(self, address, timeout, source_address=None):  # http.client's _create_connection hook
        """Resolve within the deadline, connect, and keep a **dup** of the socket for :meth:`shutdown`. The dup is our
        own descriptor, closed only by :meth:`close`, so a late watchdog never acts on a freed (and perhaps reused) fd
        number; ``shutdown`` on the dup ends the TCP connection itself, TLS handshake included. The hostname stays
        http.client's, so TLS still checks it (SNI and certificate)."""
        host, port = address
        err: OSError | None = None
        for af, kind, proto, _canon, sa in self._lookup(host, port):
            sock = socket.socket(af, kind, proto)
            try:
                self._watch_socket(sock)
            except BaseException:
                sock.close()
                raise
            try:
                sock.settimeout(max(self._deadline - self._mono(), 0.001))
                if source_address:
                    sock.bind(source_address)
                self._connect(sock, sa)
                self._check_after_connect()
            except OSError as e:
                sock.close()
                if self._shut.is_set() or self._fired.is_set() or self._mono() >= self._deadline:
                    raise
                err = e
                continue
            return sock
        raise err or OSError("no address")

    def _watch_socket(self, sock: socket.socket) -> None:
        """Make ``sock`` visible to the watchdog and :meth:`shutdown` **before** it blocks in ``connect`` (round 6,
        orch-52 #3), then check the deadline and the shutdown flag: one that passed or was set before the socket was
        installed found nothing to kill, so the call fails here at once instead of connecting."""
        watch = sock.dup()
        with self._fd_lock:
            old, self._watch = self._watch, watch
        if old is not None:
            old.close()
        if self._shut.is_set():
            raise OSError("connection shut down")
        if self._fired.is_set() or self._mono() >= self._deadline:
            raise TimeoutError("deadline passed before connect")

    def _check_after_connect(self) -> None:
        """Recheck right after ``connect`` returns, before a byte is sent (round 7, R6-2): a ``shutdown()`` (or the
        watchdog) that landed between the install check and ``connect`` hit a socket not yet connected (ENOTCONN,
        swallowed), and on Darwin a ``shutdown()`` during a pending connect can even make ``connect`` return success.
        Here the socket is connected, so a later ``shutdown()`` reaches it; one that already happened fails the call
        (the caller closes the socket). The deadline is compared on the clock too, not only through the watchdog's
        flag (round 9): a watchdog thread scheduled late must not let a call that is already past its deadline send."""
        if self._shut.is_set():
            raise OSError("connection shut down")
        if self._fired.is_set() or self._mono() >= self._deadline:
            raise TimeoutError("deadline passed during connect")

    @staticmethod
    def _connect(sock: socket.socket, sa: object) -> None:
        sock.connect(sa)

    def shutdown(self) -> None:
        """Stop this connection for good: end any blocked resolution, read or write, and fail every later call at once
        (kind ``shutdown``). Safe from another thread, never raises."""
        self._shut.set()
        self._wake.set()
        self._kill()

    def _kill(self) -> None:
        """End blocked reads and writes of the current call (the deadline watchdog)."""
        with self._fd_lock:
            w = self._watch
            if w is None:
                return
            try:
                w.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        with self._fd_lock:
            w, self._watch = self._watch, None
            conn, self._conn = self._conn, None
        for x in (conn, w):
            if x is not None:
                try:
                    x.close()
                except OSError:
                    pass

    def _open(self, timeout: float) -> http.client.HTTPConnection:
        host, port = self.proxy or (self.host, self.port)
        if self._ctx is not None:
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=self._ctx
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        if self.proxy is not None:
            conn.set_tunnel(self.host, self.port)
        conn._create_connection = self._socket  # type: ignore[attr-defined]  # keep a dup for the watchdog
        established = conn.connect

        def connect() -> None:
            # TCP, then the proxy tunnel (CONNECT) and the TLS handshake: recheck the clock once all of it is done and
            # before the first request byte (round 10, R9-2), so a slow tunnel or handshake with a late watchdog
            # cannot send past the deadline
            established()
            self._check_after_connect()

        conn.connect = connect  # type: ignore[method-assign]
        if self.proxy is not None:
            tunnel = conn._tunnel  # type: ignore[attr-defined]

            def tunnelled() -> None:  # and right after the tunnel, before the TLS ClientHello goes through it
                tunnel()
                self._check_after_connect()

            conn._tunnel = tunnelled  # type: ignore[attr-defined]
        return conn

    # -- one call -----------------------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        target: str,
        *,
        route: str,
        headers: Mapping[str, str],
        body: bytes | None,
        deadline_s: float,
        cap: int,
        resend_ok: bool = True,
    ) -> Response:
        if method not in ("GET", "POST", "PUT", "DELETE") or not _TARGET.fullmatch(target):
            raise TransportError("request", route)
        if self._allowed is not None and not self._allowed(method, target):
            raise CutOver(route)
        for k, v in headers.items():
            if not _HEADER_NAME.fullmatch(k) or type(v) is not str or not _HEADER_VALUE.fullmatch(v):
                raise TransportError("request", route)
        with self._call_lock:
            return self._request(method, target, route, headers, body, deadline_s, cap, resend_ok)

    def _remaining(self, deadline: float, route: str) -> float:
        left = deadline - self._mono()
        if left <= 0:
            raise TransportError("deadline", route)
        return left

    def _request(self, method, target, route, headers, body, deadline_s, cap, resend_ok=True) -> Response:
        """One call. A kept connection the relay has probably closed is never trusted (stale keep-alive, the relay
        closes an idle one after a few seconds): idle longer than :data:`IDLE_REUSE_S`, it is dropped before sending
        (proactive). A reused connection the peer had closed is reopened and the same bytes sent once more within the
        same deadline (reactive) only when (1) the failure came while WRITING the request (EPIPE / reset before it was
        fully sent), or (2) the request was fully sent and the answer is ``RemoteDisconnected`` (a clean EOF with an
        empty status line, the relay's idle FIN). Any other failure after the request was sent (a reset, a partial
        status line or header block) is never resent: the relay may have processed it. ``resend_ok`` false (the
        holder acquire, probe and takeover, the login challenge and login: not idempotent) never resends."""
        t0 = self._mono()
        deadline = t0 + deadline_s
        if self._conn is not None and t0 - self._last_used > IDLE_REUSE_S:
            self.close()
        reused = self._conn is not None and resend_ok
        try:
            return self._attempt(method, target, route, headers, body, deadline, cap, t0, reused=reused)
        except _Stale:
            pass  # only raised for a reused connection, which is closed by now: the next attempt opens a new one
        return self._attempt(method, target, route, headers, body, deadline, cap, t0, reused=False)

    def _attempt(self, method, target, route, headers, body, deadline, cap, t0, *, reused: bool = False) -> Response:
        if self._shut.is_set():
            raise TransportError("shutdown", route)
        self._deadline = deadline
        self._wake = threading.Event()  # per call: a stale lookup helper sets only its own call's event
        if self._shut.is_set():  # a shutdown() racing the line above
            raise TransportError("shutdown", route)
        left = self._remaining(deadline, route)
        fired = self._fired = threading.Event()

        def expire() -> None:
            fired.set()
            self._kill()

        watchdog = threading.Timer(left, expire)
        watchdog.daemon = True
        watchdog.start()
        sent = False
        try:
            if self._conn is None:
                self._conn = self._open(left)
            conn = self._conn
            conn.timeout = left
            if conn.sock is not None:
                conn.sock.settimeout(left)
            conn.putrequest(method, target, skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            sent = True
            resp = conn.getresponse()
            status = resp.status
            hdrs: dict[str, str] = {}
            for k, v in resp.getheaders():
                k = k.lower()
                hdrs[k] = f"{hdrs[k]}, {v}" if k in hdrs else v
            if 300 <= status < 400 and status != 304:
                self.close()
                raise RedirectRefused(route)
            declared = hdrs.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > cap:
                self.close()
                raise TransportError("too_large", route)
            data = self._read(conn, resp, deadline, cap, route)
            if resp.will_close:
                self.close()
        except TransportError as e:
            self.close()
            if fired.is_set() and e.kind != "redirect":
                raise TransportError("deadline", route) from None
            raise
        except (OSError, http.client.HTTPException, ValueError) as e:
            self.close()
            if (
                reused
                and (isinstance(e, http.client.RemoteDisconnected) if sent else _send_reset(e))
                and not (fired.is_set() or self._shut.is_set() or self._mono() >= deadline)
            ):
                raise _Stale from None
            raise TransportError(self._kind(e, deadline, fired), route) from None
        finally:
            watchdog.cancel()
        self._last_used = self._mono()
        log.info("relay %s status=%d bytes=%d ms=%d", route, status, len(data), int((self._mono() - t0) * 1000))
        return Response(status, hdrs, data)

    def _read(self, conn, resp, deadline: float, cap: int, route: str) -> bytes:
        parts: list[bytes] = []
        got = 0
        while True:
            left = self._remaining(deadline, route)
            if conn.sock is not None:
                conn.sock.settimeout(left)
            piece = resp.read(min(READ_PIECE, cap + 1 - got))
            if not piece:
                if resp.length:  # Content-Length promised more than the relay sent before closing
                    raise TransportError("truncated", route)
                return b"".join(parts)
            got += len(piece)
            if got > cap:
                raise TransportError("too_large", route)
            parts.append(piece)

    def _kind(self, e: BaseException, deadline: float, fired: threading.Event) -> str:
        if self._shut.is_set():
            return "shutdown"
        if isinstance(e, _ResolverStuck):
            return "connect"
        if fired.is_set() or self._mono() >= deadline:
            return "deadline"
        if isinstance(e, ssl.SSLError):
            return "tls"
        if isinstance(e, http.client.IncompleteRead):
            return "truncated"
        if isinstance(e, (ConnectionRefusedError, socket.gaierror)):
            return "connect"
        if isinstance(e, TimeoutError):
            return "deadline"
        return "protocol"
