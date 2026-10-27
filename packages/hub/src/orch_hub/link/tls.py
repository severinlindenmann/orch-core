"""TLS 1.3 transport for the link (contracts/hub/protocol.md §10.1).

The Python 3.14 stdlib ``ssl`` module has no keying-material export (only ``tls-unique`` channel binding, which does
not exist in TLS 1.3), so the link uses pyOpenSSL (a thin layer over ``cryptography``/OpenSSL) on BOTH sides:
``Connection.export_keying_material``. Trust is the pinned SPKI only: no WebPKI, no hostname check; the client
compares ``SHA-256(DER SubjectPublicKeyInfo)`` of the hub's certificate with the card's ``tls_spki_sha256`` right
after the handshake and before it sends a single byte.

All I/O is non-blocking with an explicit deadline, so no read or write can hang; an optional ``stop`` event lets a
server shut its connection threads down.
"""

from __future__ import annotations

import select
import socket
import threading
import time

from OpenSSL import SSL

from ..tlsid import TlsIdentity, spki_sha256

EXPORTER_LABEL = b"EXPORTER-orch-hub-link"
EXPORTER_LEN = 32


__all__ = ["TlsIdentity", "spki_sha256"]


class LinkClosed(Exception):
    """The peer closed the connection, the transport failed, or the server is stopping."""


class LinkTimeout(Exception):
    """A deadline passed."""


class PinMismatch(Exception):
    """The peer's certificate does not hash to the pinned SPKI."""


class TlsConn:
    """A handshaken TLS connection with deadline-bounded exact reads and full writes."""

    def __init__(self, conn: SSL.Connection, sock: socket.socket, stop: threading.Event | None = None) -> None:
        self._c, self._s, self._stop = conn, sock, stop
        # One OpenSSL object is not safe for two threads at once. The router writes route_in frames from another
        # link's thread while this link's own thread reads, so each single SSL call (never a wait) holds this lock.
        self._io = threading.Lock()

    def same_computer(self) -> bool:
        """True when the peer is on this computer, as the socket itself shows: a loopback peer, or one whose address is
        the very address it dialled (a computer reaching its own private IP). Never from anything the peer sent."""
        try:
            import ipaddress

            peer = ipaddress.ip_address(self._s.getpeername()[0].split("%")[0])
            mine = ipaddress.ip_address(self._s.getsockname()[0].split("%")[0])
        except OSError, ValueError, IndexError:
            return False
        peer, mine = getattr(peer, "ipv4_mapped", None) or peer, getattr(mine, "ipv4_mapped", None) or mine
        return peer.is_loopback or peer == mine

    def _wait(self, write: bool, deadline: float) -> None:
        while True:
            if self._stop is not None and self._stop.is_set():
                raise LinkClosed("stopping")
            left = deadline - time.monotonic()
            if left <= 0:
                raise LinkTimeout
            r, w, _ = select.select([] if write else [self._s], [self._s] if write else [], [], min(left, 0.2))
            if r or w:
                return

    def _run(self, fn, deadline: float):
        while True:
            try:
                with self._io:
                    return fn()
            except SSL.WantReadError:
                self._wait(False, deadline)
            except SSL.WantWriteError:
                self._wait(True, deadline)
            except (SSL.Error, OSError) as e:
                raise LinkClosed(type(e).__name__) from None

    def handshake(self, deadline: float) -> None:
        self._run(self._c.do_handshake, deadline)

    def recv_exact(self, n: int, deadline: float) -> bytes:
        out = bytearray()
        while len(out) < n:
            want = n - len(out)
            chunk = self._run(lambda k=want: self._c.recv(k), deadline)
            if not chunk:
                raise LinkClosed("eof")
            out += chunk
        return bytes(out)

    def send_all(self, data: bytes, deadline: float) -> None:
        view, off = memoryview(data), 0
        while off < len(view):
            off += self._run(lambda part=view[off:]: self._c.send(part), deadline)

    def exporter(self) -> bytes:
        """The TLS 1.3 exporter, label ``EXPORTER-orch-hub-link``, empty context, 32 bytes (§10.1)."""
        return self._c.export_keying_material(EXPORTER_LABEL, EXPORTER_LEN, b"")

    def peer_spki_sha256(self) -> str:
        cert = self._c.get_peer_certificate(as_cryptography=True)
        if cert is None:
            raise PinMismatch
        return spki_sha256(cert)

    def close(self) -> None:
        try:
            self._s.close()
        except OSError:
            pass


def _context(method: int) -> SSL.Context:
    ctx = SSL.Context(method)
    ctx.set_min_proto_version(SSL.TLS1_3_VERSION)
    ctx.set_max_proto_version(SSL.TLS1_3_VERSION)
    ctx.set_verify(SSL.VERIFY_NONE)  # trust is the SPKI pin, checked by the caller after the handshake
    return ctx


def server_context(identity: TlsIdentity) -> SSL.Context:
    ctx = _context(SSL.TLS_SERVER_METHOD)
    ctx.use_privatekey(identity.key)
    ctx.use_certificate(identity.cert)
    return ctx


def accept(ctx: SSL.Context, sock: socket.socket, deadline: float, stop: threading.Event | None = None) -> TlsConn:
    sock.setblocking(False)
    c = SSL.Connection(ctx, sock)
    c.set_accept_state()
    t = TlsConn(c, sock, stop)
    t.handshake(deadline)
    return t


def connect_pinned(
    host: str, port: int, pinned_spki_sha256: str, *, timeout: float = 10.0, stop: threading.Event | None = None
) -> TlsConn:
    """Dial, handshake TLS 1.3 and compare the peer certificate's SPKI hash with the pin; closes and raises
    :class:`PinMismatch` on any difference, before anything is sent."""
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise LinkClosed(type(e).__name__) from None
    sock.setblocking(False)
    c = SSL.Connection(_context(SSL.TLS_CLIENT_METHOD), sock)
    c.set_connect_state()
    t = TlsConn(c, sock, stop)
    try:
        t.handshake(deadline)
        if t.peer_spki_sha256() != pinned_spki_sha256:
            raise PinMismatch
    except BaseException:
        t.close()
        raise
    return t
