"""TLS 1.3 transport of the host end of the hub link (``contracts/hub/protocol.md`` section 10.1).

The Python 3.14 ``ssl`` module has no keying-material exporter, so the link uses pyOpenSSL
(``Connection.export_keying_material``; V3-D15). This is the only module in core that imports it. Trust is the pinned
SPKI of the hub card (``tls_spki_sha256``) and nothing else: no WebPKI, no hostname check. The pin is compared right
after the handshake and before a single byte is sent. All I/O is non-blocking with an explicit deadline, so no read or
write can hang.
"""

from __future__ import annotations

import hmac
import socket
import time
from functools import partial

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from OpenSSL import SSL

from orch import fdwait
from orch.crypto.encoding import EncodingError, b64u, unb64u

from ..linkaddr import LinkAddr
from ..netconfig import NetSettings
from . import transport
from .errors import LinkClosed, LinkInspected, LinkTimeout, NoPin, PinMismatch

__all__ = [
    "EXPORTER_LABEL",
    "EXPORTER_LEN",
    "BioTlsConn",
    "LinkClosed",
    "LinkInspected",
    "LinkTimeout",
    "PLACEHOLDER_PIN",
    "NoPin",
    "PinMismatch",
    "TlsConn",
    "connect_pinned",
    "dial_link",
    "spki_sha256",
]

EXPORTER_LABEL = b"EXPORTER-orch-hub-link"
EXPORTER_LEN = 32


#: What an orch-hub card carries before it has a TLS key (``b64u(SHA-256("orch-hub dev: no TLS key pinned yet"))``).
#: It matches no real key; a host never dials on it (D12b G6).
PLACEHOLDER_PIN = "l35Tp0Xpilo2yI3r8TVl3Q9_pQ9QB8neaBC5A3dNG6k"


def spki_sha256(cert: x509.Certificate) -> str:
    """b64u of SHA-256 over the DER SubjectPublicKeyInfo: the form of the card's ``tls_spki_sha256``."""
    der = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashes.Hash(hashes.SHA256())
    h.update(der)
    return b64u(h.finalize())


class TlsConn:
    """A handshaken TLS connection with deadline-bounded exact reads and full writes."""

    def __init__(self, conn: SSL.Connection, sock: socket.socket) -> None:
        self._c, self._s = conn, sock

    def _wait(self, write: bool, deadline: float) -> None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise LinkTimeout
        if not (fdwait.writable if write else fdwait.readable)(self._s, left):
            raise LinkTimeout

    def _run(self, fn, deadline: float):
        while True:
            try:
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
            chunk = self._run(partial(self._c.recv, n - len(out)), deadline)
            if not chunk:
                raise LinkClosed("eof")
            out += chunk
        return bytes(out)

    def send_all(self, data: bytes, deadline: float) -> None:
        view, off = memoryview(data), 0
        while off < len(view):
            off += self._run(partial(self._c.send, view[off:]), deadline)

    def wait_readable(self, timeout: float) -> bool:
        """True when a frame has started to arrive (decrypted bytes buffered, or the socket is readable). Lets a
        poller wait without a read deadline cutting a frame in half."""
        try:
            if self._c.pending() > 0:
                return True
        except SSL.Error:
            return True  # let the read report the failure
        return fdwait.readable(self._s, max(timeout, 0.0))

    def exporter(self) -> bytes:
        """The TLS 1.3 exporter: label ``EXPORTER-orch-hub-link``, empty context, 32 bytes (section 10.1)."""
        return self._c.export_keying_material(EXPORTER_LABEL, EXPORTER_LEN, b"")

    def alpn(self) -> bytes:
        """The ALPN protocol negotiated in the handshake, ``b""`` if none."""
        return self._c.get_alpn_proto_negotiated() or b""

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


class BioTlsConn(TlsConn):
    """The link's TLS over a byte pipe instead of a socket (a WebSocket, a tunnel inside an ``https`` proxy): a
    pyOpenSSL memory-BIO connection, the same interface as :class:`TlsConn`. Pump rules:

    1. Pull, never push: wire bytes are read from the pipe only when OpenSSL raises ``WantRead``, then ``bio_write``.
    2. After every SSL call, reads and handshake included, the output BIO is drained to the pipe (TLS 1.3 sends
       tickets and key updates from inside a read).
    3. One thread at a time, as the client's lock gives it.
    4. :meth:`wait_readable` is true only when decrypted bytes exist; pings, pongs and handshake records alone never
       make it true (a ping every 25 s would otherwise start a frame read that times out and drops the link).
    """

    def __init__(self, conn: SSL.Connection, pipe: transport.Pipe) -> None:
        super().__init__(conn, None)  # type: ignore[arg-type]
        self._p = pipe

    def _flush(self, deadline: float) -> None:
        while True:
            try:
                data = self._c.bio_read(16384)
            except SSL.WantReadError:
                return
            self._p.write(data, deadline)

    def _run(self, fn, deadline: float):  # type: ignore[no-untyped-def]
        while True:
            try:
                out = fn()
            except SSL.WantReadError:
                self._flush(deadline)
                self._c.bio_write(self._p.read(deadline))
                continue
            except SSL.WantWriteError:
                self._flush(deadline)
                continue
            except (SSL.Error, OSError) as e:
                raise LinkClosed(type(e).__name__) from None
            self._flush(deadline)
            return out

    def wait_readable(self, timeout: float) -> bool:
        end = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                if self._c.pending() > 0:
                    return True
                try:
                    self._c.recv(1, socket.MSG_PEEK)
                    return True
                except (SSL.WantReadError, SSL.WantWriteError):
                    pass
                self._flush(time.monotonic() + 10.0)
            except (SSL.Error, OSError):
                return True  # let the read report the failure
            try:
                raw = self._p.poll(max(end - time.monotonic(), 0.0))
            except (LinkClosed, LinkTimeout):
                return True
            if raw is None:
                return False
            if raw:
                self._c.bio_write(raw)
            elif time.monotonic() >= end:
                return False

    def close(self) -> None:
        self._p.close()


def client_context(alpn: tuple[bytes, ...] = ()) -> SSL.Context:
    ctx = SSL.Context(SSL.TLS_CLIENT_METHOD)
    if alpn:
        ctx.set_alpn_protos(list(alpn))
    ctx.set_min_proto_version(SSL.TLS1_3_VERSION)
    ctx.set_max_proto_version(SSL.TLS1_3_VERSION)
    ctx.set_verify(SSL.VERIFY_NONE)  # trust is the SPKI pin, compared by dial_link before anything is sent
    return ctx


def dial_link(
    addr: LinkAddr,
    pinned_spki_sha256: str,
    *,
    timeout: float = 10.0,
    alpn: tuple[bytes, ...] = (),
    net: NetSettings | None = None,
    connect_host: str | None = None,
) -> TlsConn:
    """THE dial: every byte of a hub link leaves the host through here. Reach ``addr`` as its scheme and the host's
    proxy settings ``net`` say (:mod:`.transport`), run the link's TLS 1.3 handshake (SNI = the address's DNS name,
    none for an IP) and compare the peer's SPKI hash with the pin before anything is sent. A mismatch is
    :class:`PinMismatch`; :class:`LinkInspected` when a proxy or a WebSocket front was in the way. No pin, a malformed
    pin or the placeholder is :class:`NoPin` (nothing dialled). There is no fallback to another transport, around the
    proxy, or to an unpinned or plaintext connection. ``connect_host`` is the checked IP of a name, used for a direct
    TCP dial only."""
    if type(pinned_spki_sha256) is not str or pinned_spki_sha256 == PLACEHOLDER_PIN:
        raise NoPin
    try:
        unb64u(pinned_spki_sha256, 32)
    except EncodingError:
        raise NoPin from None
    deadline = time.monotonic() + timeout
    path = transport.open_path(addr, net, deadline, connect_host=connect_host)
    try:
        c: SSL.Connection
        t: TlsConn
        if path.pipe is None:
            path.sock.setblocking(False)
            c = SSL.Connection(client_context(alpn), path.sock)
            t = TlsConn(c, path.sock)
        else:
            c = SSL.Connection(client_context(alpn), None)
            t = BioTlsConn(c, path.pipe)
        if addr.sni is not None:
            c.set_tlsext_host_name(addr.sni.encode("ascii"))
        c.set_connect_state()
        t.handshake(deadline)
        if not hmac.compare_digest(t.peer_spki_sha256().encode(), pinned_spki_sha256.encode()):
            raise LinkInspected if path.via else PinMismatch
    except BaseException:
        path.close()
        raise
    return t


def connect_pinned(
    host: str,
    port: int,
    pinned_spki_sha256: str,
    *,
    timeout: float = 10.0,
    alpn: tuple[bytes, ...] = (),
    net: NetSettings | None = None,
) -> TlsConn:
    """:func:`dial_link` for a direct ``host:port`` (a convenience; not a second dial path)."""
    h = "[" + host + "]" if ":" in host else host
    return dial_link(
        LinkAddr("tcp", host, port, f"{h}:{port}"), pinned_spki_sha256, timeout=timeout, alpn=alpn, net=net
    )
