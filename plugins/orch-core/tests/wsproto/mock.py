"""A recorded-fixture relay on loopback (11A.10 harness layer 1): an in-process ``http.server`` on ``127.0.0.1`` with an
ephemeral port, optionally behind TLS with a throwaway CA. Each expected request is matched (method, path, query, the
headers that carry meaning, the exact body bytes) and answered with a fixture response or a scripted fault.

No external network. Every server thread is a daemon and the server is shut down by the fixture that started it."""

from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from orch import canon

from . import corpus

MEANINGFUL = ("authorization", "orch-holder", "content-type")


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    body: bytes = b"{}"
    fault: str | None = None  # drip_headers | drip_body | truncate | chunked | close | hang
    drip_s: float = 0.05


@dataclass
class Expect:
    method: str
    path: str
    query: dict[str, str] | None = None
    headers: dict[str, str] | None = None  # meaningful headers that must be present with these values (others absent)
    body: bytes | None | Callable[[bytes], bool] = None
    reply: Reply | Callable[[Seen], Reply] = field(default_factory=Reply)


def json_reply(status: int, obj: Any, headers: dict[str, str] | None = None) -> Reply:
    h = {"Content-Type": "application/json", **(headers or {})}
    return Reply(status=status, headers=h, body=canon.dumps(obj))


def fixture_expect(name: str, case_name: str, *, body: Any = "exact", reply: Reply | None = None) -> Expect:
    """An :class:`Expect` from a fixture case: the request matched as recorded, the recorded response returned."""
    c = corpus.case(name, case_name)
    req, resp = c["request"], c["response"]
    rb: Any = corpus.request_body(req) if body == "exact" else body
    out = reply or json_reply(resp["status"], resp.get("json"), {k: v for k, v in resp["headers"].items()})
    if body is None:  # reply-only: the recorded refusal is replayed for whatever the client sends on that route
        return Expect(req["method"], req["path"], None, None, None, out)
    return Expect(req["method"], req["path"], req["query"], dict(req["headers"]), rb, out)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *a: Any) -> None:  # quiet
        pass

    def _serve(self) -> None:
        mock = self.server.mock
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 64 << 20)) if n else b""
        u = urlsplit(self.path)
        seen = Seen(
            self.command,
            u.path,
            dict(parse_qsl(u.query, keep_blank_values=True)),
            {k.lower(): v for k, v in self.headers.items()},
            body,
        )
        mock.seen.append(seen)
        reply = mock.match(seen)
        self._send(reply)

    do_GET = do_POST = do_PUT = do_DELETE = _serve

    def _send(self, r: Reply) -> None:
        w = self.wfile
        head = f"HTTP/1.1 {r.status} X\r\n".encode()
        headers = dict(r.headers)
        if r.fault == "chunked":
            headers["Transfer-Encoding"] = "chunked"
        elif r.fault == "truncate":
            headers["Content-Length"] = str(len(r.body) + 100)
        else:
            headers.setdefault("Content-Length", str(len(r.body)))
        head += b"".join(f"{k}: {v}\r\n".encode() for k, v in headers.items()) + b"\r\n"
        try:
            if r.fault == "hang":
                time.sleep(30)
                return
            if r.fault == "drip_headers":
                for i in range(len(head)):
                    w.write(head[i : i + 1])
                    w.flush()
                    time.sleep(r.drip_s)
            else:
                w.write(head)
            if r.fault == "drip_body":
                for i in range(len(r.body)):
                    w.write(r.body[i : i + 1])
                    w.flush()
                    time.sleep(r.drip_s)
            elif r.fault == "chunked":
                for i in range(0, len(r.body), 4096):
                    part = r.body[i : i + 4096]
                    w.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                w.write(b"0\r\n\r\n")
            elif r.fault != "drip_headers":
                w.write(r.body)
            w.flush()
            if r.fault in ("truncate", "close"):
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            self.close_connection = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    mock: MockRelay

    def handle_error(self, request, client_address) -> None:  # a client that hung up mid-reply: expected here
        pass


class MockRelay:
    def __init__(self, tls: ssl.SSLContext | None = None, host: str = "127.0.0.1") -> None:
        self.script: deque[Expect] = deque()
        self.seen: list[Seen] = []
        self.errors: list[str] = []
        self.fallback: Callable[[Seen], Reply] | None = None  # answers when the script is empty (a simulated relay)
        self._srv = _Server((host, 0), _Handler)
        self._srv.mock = self
        if tls is not None:
            self._srv.socket = tls.wrap_socket(self._srv.socket, server_side=True)
        self.port = self._srv.server_address[1]
        self.scheme = "https" if tls else "http"
        self._t = threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._t.start()

    def origin(self, host: str = "127.0.0.1") -> str:
        return f"{self.scheme}://{host}:{self.port}"

    def expect(self, *e: Expect) -> MockRelay:
        self.script.extend(e)
        return self

    def match(self, s: Seen) -> Reply:
        if not self.script and self.fallback is not None:
            return self.fallback(s)
        if not self.script:
            self.errors.append(f"unexpected {s.method} {s.path}")
            return json_reply(599, {"error": "unexpected"})
        e = self.script.popleft()
        problems = []
        if (e.method, e.path) != (s.method, s.path):
            problems.append(f"route {s.method} {s.path} != {e.method} {e.path}")
        if e.query is not None and e.query != s.query:
            problems.append(f"query {s.query} != {e.query}")
        if e.headers is not None:
            want = {k.lower(): v for k, v in e.headers.items() if k.lower() in MEANINGFUL}
            got = {k: s.headers[k] for k in MEANINGFUL if k in s.headers}
            if want != got:
                problems.append(f"headers {got} != {want}")
        if callable(e.body):
            if not e.body(s.body):
                problems.append("body check failed")
        elif e.body is not None and e.body != s.body:
            problems.append(f"body {s.body[:200]!r} != {e.body[:200]!r}")
        if problems:
            self.errors.append("; ".join(problems))
        return e.reply(s) if callable(e.reply) else e.reply

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._t.join(5)


# --- a throwaway CA for TLS tests ---------------------------------------------------------------------------------


def make_tls(
    tmp: Path, *, names: tuple[str, ...] = ("localhost",), expired: bool = False
) -> tuple[ssl.SSLContext, Path]:
    """A server context for ``names`` and ``127.0.0.1`` signed by a fresh CA, and the CA file for the client;
    ``expired`` makes the server certificate's validity end a day ago."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "orch test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2 if expired else 0, minutes=5))
        .not_valid_after(now + datetime.timedelta(days=-1 if expired else 1))
        .add_extension(
            x509.SubjectAlternativeName(
                [*(x509.DNSName(n) for n in names), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file = tmp / "ca.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file, key_file = tmp / "cert.pem", tmp / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx, ca_file


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
