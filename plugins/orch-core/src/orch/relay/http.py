"""The stdlib relay transport (contract 11A.7): one :class:`RelayConn` per thread.

* ``http.client`` only. It reads no proxy variable from the environment; a proxy is used only when ``link.json``
  names one (``host:port``), through ``set_tunnel``.
* TLS: ``ssl.create_default_context()`` (``CERT_REQUIRED``, hostname check, TLS >= 1.2), or the ``ca_file`` that
  ``link.json`` names. When the default context trusts nothing (``get_ca_certs()`` empty, the macOS python.org case)
  and no ``ca_file`` is named, construction fails with :class:`NoTrustStore` (alarm ``tls_no_trust_store``). There is
  no unverified fallback. Plain ``http`` is only for the loopback origins that :func:`wire.check_origin` allows.
* Every call has a **total** deadline on the injected monotonic clock covering connect, TLS, headers and body: a
  watchdog shuts the socket down when it passes (so a relay that drips header bytes cannot stretch a call), and the
  socket timeout is set to the time remaining before every bounded body read.
* The body is read in bounded pieces against ``cap`` whatever ``Content-Length`` or chunked framing says; over the cap
  the connection is closed and the call fails as transient ``too_large``.
* No redirect is followed: any 3xx is :class:`RedirectRefused` (permanent; alarm ``relay_redirect``).
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

__all__ = ["NoTrustStore", "RedirectRefused", "RelayConn", "Response", "Transport", "TransportError", "tls_context"]

log = logging.getLogger("orch.relay.http")

READ_PIECE = 64 * 1024
_HEADER_VALUE = re.compile(r"[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?")
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
_TARGET = re.compile(r"/[A-Za-z0-9._~%/:@!$&'()*+,;=?-]{0,2047}")
_PROXY = re.compile(r"(?P<host>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?):(?P<port>[1-9][0-9]{0,4})")


class TransportError(Exception):
    """A call that produced no relay answer. ``kind``: ``connect``, ``tls``, ``deadline``, ``too_large``,
    ``truncated``, ``protocol``, ``request`` (refused locally) or ``redirect``. Transient unless stated."""

    transient = True

    def __init__(self, kind: str, route: str) -> None:
        super().__init__(f"relay transport {kind} on {route}")
        self.kind, self.route = kind, route


class RedirectRefused(TransportError):
    transient = False

    def __init__(self, route: str) -> None:
        super().__init__("redirect", route)


class NoTrustStore(Exception):
    """No CA is trusted and ``link.json`` names no ``ca_file``: the worker refuses to start (``tls_no_trust_store``)."""


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
    ) -> Response: ...

    def shutdown(self) -> None: ...


def tls_context(ca_file: str | None, *, factory: Callable[..., ssl.SSLContext] = ssl.create_default_context):
    """A verifying client context; refuses an empty trust store when no ``ca_file`` is named."""
    ctx = factory(cafile=ca_file) if ca_file else factory()
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    if ctx.minimum_version < ssl.TLSVersion.TLSv1_2:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not ca_file and not ctx.get_ca_certs():
        raise NoTrustStore("no CA certificates are trusted and link.json names no ca_file")
    return ctx


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
    ) -> None:
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
        self._ctx = tls_context(ca_file, factory=context_factory) if self.scheme == "https" else None
        self._mono = mono
        self._conn: http.client.HTTPConnection | None = None
        self._fd: int | None = None
        self._fd_lock = threading.Lock()
        self._call_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"RelayConn({self.origin})"

    # -- sockets ------------------------------------------------------------------------------------------------

    def _socket(self, address, timeout, source_address=None):  # http.client's _create_connection hook
        sock = socket.create_connection(address, timeout, source_address)
        with self._fd_lock:
            self._fd = sock.fileno()
        return sock

    def shutdown(self) -> None:
        """End any blocked read or write on this connection (safe from another thread)."""
        with self._fd_lock:
            fd = self._fd
            if fd is None:
                return
            s = socket.socket(fileno=fd)
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                s.detach()

    def close(self) -> None:
        with self._fd_lock:
            self._fd = None
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
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
        conn._create_connection = self._socket  # type: ignore[attr-defined]  # record the fd for the watchdog
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
    ) -> Response:
        if method not in ("GET", "POST", "PUT", "DELETE") or not _TARGET.fullmatch(target):
            raise TransportError("request", route)
        for k, v in headers.items():
            if not _HEADER_NAME.fullmatch(k) or type(v) is not str or not _HEADER_VALUE.fullmatch(v):
                raise TransportError("request", route)
        with self._call_lock:
            return self._request(method, target, route, headers, body, deadline_s, cap)

    def _remaining(self, deadline: float, route: str) -> float:
        left = deadline - self._mono()
        if left <= 0:
            raise TransportError("deadline", route)
        return left

    def _request(self, method, target, route, headers, body, deadline_s, cap) -> Response:
        t0 = self._mono()
        deadline = t0 + deadline_s
        left = self._remaining(deadline, route)
        fired = threading.Event()

        def expire() -> None:
            fired.set()
            self.shutdown()

        watchdog = threading.Timer(left, expire)
        watchdog.daemon = True
        watchdog.start()
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
            resp = conn.getresponse()
            status = resp.status
            hdrs: dict[str, str] = {}
            for k, v in resp.getheaders():
                k = k.lower()
                hdrs[k] = f"{hdrs[k]}, {v}" if k in hdrs else v
            if 300 <= status < 400:
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
            raise TransportError(self._kind(e, deadline, fired), route) from None
        finally:
            watchdog.cancel()
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
