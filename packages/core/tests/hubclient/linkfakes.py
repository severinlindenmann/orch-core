"""In-process fakes for the link transports (R6 #1594): a terminating WebSocket front, an HTTP CONNECT proxy and the
throwaway certificate they use for outer TLS. They relay raw bytes to a real ``tests.hub.tlshub.TlsHub``; none of them
ever sees anything but the link's own TLS records. Every fake counts what it saw so a test can prove what did NOT
happen (no second dial, no byte at the real hub)."""

from __future__ import annotations

import base64
import datetime
import ipaddress
import socket
import ssl
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from orch.hubclient.tlslink import wsframe as W


class Pki:
    """One self-signed certificate for 127.0.0.1, ``localhost`` and ``hub.test``; the client side trusts exactly it."""

    def __init__(self, directory: Path) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "front")])
        now = datetime.datetime.now(datetime.UTC)
        self.cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost"),
                     x509.DNSName("hub.test")]
                ), critical=False,
            )
            .sign(key, hashes.SHA256())
        )  # fmt: skip
        self.pem = self.cert.public_bytes(serialization.Encoding.PEM)
        self.cert_file, self.key_file = directory / "front.pem", directory / "front.key"
        self.cert_file.write_bytes(self.pem)
        self.key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )

    def server_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_file, self.key_file)
        return ctx

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cadata=self.pem.decode())


def read_head(sock: socket.socket, limit: int = 16384) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = sock.recv(4096)
        if not d:
            break
        buf += d
        if len(buf) > limit:
            break
    return buf


def parse_head(head: bytes) -> tuple[str, dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    fields = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        if k:
            fields[k.strip().lower()] = v.strip()
    return lines[0], fields


def relay(a: socket.socket, b: socket.socket, stop: threading.Event) -> None:
    """Copy bytes both ways until either side ends."""

    def run(src: socket.socket, dst: socket.socket) -> None:
        try:
            while not stop.is_set():
                try:
                    d = src.recv(65536)
                except TimeoutError:
                    continue
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    t = threading.Thread(target=run, args=(b, a), daemon=True)
    t.start()
    run(a, b)


class Listener:
    def __init__(self, pki: Pki | None = None) -> None:
        self._ctx = pki.server_context() if pki else None
        self._ls = socket.socket()
        self._ls.bind(("127.0.0.1", 0))
        self._ls.listen(16)
        self._ls.settimeout(0.1)
        self.stop = threading.Event()
        self.accepted = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._ls.getsockname()[1]

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                s, _ = self._ls.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._guard, args=(s,), daemon=True).start()

    def _guard(self, s: socket.socket) -> None:
        try:
            s.settimeout(5)
            if self._ctx is not None:
                s = self._ctx.wrap_socket(s, server_side=True)
            s.settimeout(0.2)
            self.handle(s)
        except Exception:  # noqa: BLE001 - a fake drops a connection on anything
            pass
        finally:
            try:
                s.close()
            except OSError:
                pass

    def handle(self, s: socket.socket) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        self.stop.set()
        self._thread.join(2)
        self._ls.close()


class WsFront(Listener):
    """Terminates (outer TLS when ``pki``) the WebSocket upgrade and carries the binary frames to ``target`` (the hub's
    link port). ``misbehave`` selects one fault: ``redirect``, ``bad_accept``, ``bad_proto``, ``extensions``,
    ``status200``, or, right after a good 101, a bad frame: ``text``, ``rsv``, ``oversize``, ``masked``."""

    def __init__(
        self, target: int, *, pki: Pki | None = None, ping_interval: float | None = None,
        misbehave: str | None = None, no_pong: bool = False, silent: bool = False,
    ) -> None:  # fmt: skip
        self.target, self.ping_interval, self.misbehave = target, ping_interval, misbehave
        self.no_pong, self.silent = no_pong, silent
        self.heads: list[bytes] = []
        self.client_frames: list[tuple[int, int]] = []  # (opcode, payload length) of every client frame, all masked
        self.errors: list[W.WsError] = []
        self.pings = 0
        self.target_dials = 0
        super().__init__(pki)

    def _respond(self, key: str) -> bytes:
        acc = W.accept_key(key)
        if self.misbehave == "redirect":
            return b"HTTP/1.1 302 Found\r\nLocation: wss://elsewhere.example/orch/link/1\r\n\r\n"
        if self.misbehave == "status200":
            return b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        if self.misbehave == "bad_accept":
            acc = W.accept_key("AAAAAAAAAAAAAAAAAAAAAA==")
        proto = "chat" if self.misbehave == "bad_proto" else W.SUBPROTOCOL
        extra = "Sec-WebSocket-Extensions: permessage-deflate\r\n" if self.misbehave == "extensions" else ""
        return (
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {acc}\r\nSec-WebSocket-Protocol: {proto}\r\n{extra}\r\n"
        ).encode()

    def handle(self, s: socket.socket) -> None:
        head = read_head(s)
        self.heads.append(head)
        line, fields = parse_head(head)
        if not line.startswith("GET /orch/link/1 HTTP/1.1"):
            s.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        s.sendall(self._respond(fields.get("sec-websocket-key", "")))
        bad = {
            "text": b"\x81\x00", "rsv": b"\xc1\x00", "masked": b"\x82\x80\x01\x02\x03\x04",
            "oversize": b"\x82\x7f" + (W.MAX_FRAME + 1).to_bytes(8, "big"),
        }.get(self.misbehave or "")  # fmt: skip
        if bad is not None:
            s.sendall(bad)
            self.stop.wait(1.0)
            return
        if self.misbehave or self.silent:
            self.stop.wait(1.0)
            return
        self.target_dials += 1
        hub = socket.create_connection(("127.0.0.1", self.target))
        hub.settimeout(0.2)
        lock = threading.Lock()
        done = threading.Event()

        def send(op: int, payload: bytes) -> None:
            with lock:
                s.sendall(W.encode_frame(op, payload))

        def pinger() -> None:
            while not done.wait(self.ping_interval or 3600):
                self.pings += 1
                try:
                    send(W.PING, b"p")
                except OSError:
                    return

        if self.ping_interval:
            threading.Thread(target=pinger, daemon=True).start()

        def hub_to_client() -> None:
            try:
                while not done.is_set() and not self.stop.is_set():
                    try:
                        d = hub.recv(65536)
                    except TimeoutError:
                        continue
                    if not d:
                        break
                    for i in range(0, len(d), W.MAX_SEND_CHUNK):
                        send(W.BINARY, d[i : i + W.MAX_SEND_CHUNK])
            except OSError:
                pass
            finally:
                done.set()

        threading.Thread(target=hub_to_client, daemon=True).start()
        buf = b""
        try:
            while not done.is_set() and not self.stop.is_set():
                try:
                    d = s.recv(65536)
                except TimeoutError:
                    continue
                if not d:
                    break
                buf += d
                while True:
                    try:
                        got = W.decode_frame(buf, sender="client")
                    except W.WsError as e:
                        self.errors.append(e)
                        return
                    if got is None:
                        break
                    f, used = got
                    buf = buf[used:]
                    self.client_frames.append((f.opcode, len(f.payload)))
                    if f.opcode in (W.BINARY, W.CONT):
                        hub.sendall(f.payload)
                    elif f.opcode == W.PING and not self.no_pong:
                        send(W.PONG, f.payload)
                    elif f.opcode == W.CLOSE:
                        return
        finally:
            done.set()
            hub.close()


class ConnectProxy(Listener):
    """An HTTP proxy that does CONNECT only. ``routes`` maps the requested ``name:port`` to a local port (the proxy,
    not the client, resolves names). ``auth`` = (user, password) asks for Basic; ``mode`` forces an answer:
    ``407_ntlm``, ``403``, ``garbage``."""

    def __init__(
        self, routes: dict[str, int], *, pki: Pki | None = None, auth: tuple[str, str] | None = None,
        mode: str | None = None,
    ) -> None:  # fmt: skip
        self.routes, self.auth, self.mode = routes, auth, mode
        self.requests: list[bytes] = []
        self.tunnels = 0
        super().__init__(pki)

    def handle(self, s: socket.socket) -> None:
        head = read_head(s)
        self.requests.append(head)
        line, fields = parse_head(head)
        parts = line.split(" ")
        if len(parts) != 3 or parts[0] != "CONNECT":
            s.sendall(b"HTTP/1.1 405 No\r\n\r\n")
            return
        if self.mode == "garbage":
            s.sendall(b"\x00\x01 not http at all\r\n\r\n")
            return
        if self.mode == "403":
            s.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        if self.mode == "407_ntlm":
            s.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: NTLM\r\n"
                b"Proxy-Authenticate: Negotiate\r\nContent-Length: 0\r\n\r\n"
            )
            return
        if self.auth is not None:
            want = "Basic " + base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode()).decode()
            if fields.get("proxy-authorization") != want:
                s.sendall(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="x"\r\n'
                    b"Content-Length: 0\r\n\r\n"
                )
                return
        port = self.routes.get(parts[1])
        if port is None:
            s.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        up = socket.create_connection(("127.0.0.1", port))
        up.settimeout(0.2)
        s.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.tunnels += 1
        relay(s, up, self.stop)


def closed_port() -> int:
    """A port nothing listens on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    time.sleep(0.01)
    return p
