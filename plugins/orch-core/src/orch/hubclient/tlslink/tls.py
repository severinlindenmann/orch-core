"""TLS 1.3 transport of the host end of the hub link (orch-hub ``docs/protocol.md`` section 10.1).

The Python 3.14 ``ssl`` module has no keying-material exporter, so the link uses pyOpenSSL
(``Connection.export_keying_material``; V3-D15). This is the only module in core that imports it. Trust is the pinned
SPKI of the hub card (``tls_spki_sha256``) and nothing else: no WebPKI, no hostname check. The pin is compared right
after the handshake and before a single byte is sent. All I/O is non-blocking with an explicit deadline, so no read or
write can hang.
"""

from __future__ import annotations

import hmac
import socket
import threading
import time
from functools import partial

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from OpenSSL import SSL

from orch import fdwait
from orch.crypto.encoding import EncodingError, b64u, unb64u

__all__ = [
    "EXPORTER_LABEL",
    "EXPORTER_LEN",
    "LinkClosed",
    "LinkTimeout",
    "PLACEHOLDER_PIN",
    "NoPin",
    "PinMismatch",
    "TlsConn",
    "connect_pinned",
    "spki_sha256",
]

EXPORTER_LABEL = b"EXPORTER-orch-hub-link"
EXPORTER_LEN = 32


class LinkClosed(ConnectionError):
    """The peer closed the connection or the transport failed."""


class LinkTimeout(TimeoutError):
    """A deadline passed."""


#: What an orch-hub card carries before it has a TLS key (``b64u(SHA-256("orch-hub dev: no TLS key pinned yet"))``).
#: It matches no real key; a host never dials on it (D12b G6).
PLACEHOLDER_PIN = "l35Tp0Xpilo2yI3r8TVl3Q9_pQ9QB8neaBC5A3dNG6k"


class PinMismatch(Exception):
    """The peer's certificate does not hash to the pinned SPKI (nothing was sent)."""


class NoPin(PinMismatch):
    """There is no usable pin to dial with (missing, malformed or the card's placeholder): nothing was dialled."""


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


def client_context() -> SSL.Context:
    ctx = SSL.Context(SSL.TLS_CLIENT_METHOD)
    ctx.set_min_proto_version(SSL.TLS1_3_VERSION)
    ctx.set_max_proto_version(SSL.TLS1_3_VERSION)
    ctx.set_verify(SSL.VERIFY_NONE)  # trust is the SPKI pin, compared by connect_pinned before anything is sent
    return ctx


def _dial(host: str, port: int, deadline: float) -> socket.socket:
    """Resolve and connect within the overall deadline (the resolver itself cannot be interrupted, so it runs in a
    helper thread that is abandoned on timeout; a socket it opens late is closed)."""
    out: list[socket.socket | BaseException] = []
    done = threading.Event()
    abandoned = threading.Event()

    def work() -> None:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
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


def connect_pinned(host: str, port: int, pinned_spki_sha256: str, *, timeout: float = 10.0) -> TlsConn:
    """Dial, handshake TLS 1.3 and compare the peer certificate's SPKI hash with the pin. Raises
    :class:`PinMismatch` (connection closed, nothing sent) on any difference; a malformed pin is refused before
    dialling. No pin, a malformed pin or the placeholder pin is :class:`NoPin`: the host never dials without a real
    pin and never falls back to an unpinned or plaintext connection."""
    if type(pinned_spki_sha256) is not str or pinned_spki_sha256 == PLACEHOLDER_PIN:
        raise NoPin
    try:
        unb64u(pinned_spki_sha256, 32)
    except EncodingError:
        raise NoPin from None
    deadline = time.monotonic() + timeout
    sock = _dial(host, port, deadline)
    sock.setblocking(False)
    c = SSL.Connection(client_context(), sock)
    c.set_connect_state()
    t = TlsConn(c, sock)
    try:
        t.handshake(deadline)
        if not hmac.compare_digest(t.peer_spki_sha256().encode(), pinned_spki_sha256.encode()):
            raise PinMismatch
    except BaseException:
        t.close()
        raise
    return t
