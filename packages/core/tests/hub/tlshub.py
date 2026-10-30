"""A TLS test hub for the link client tests: pyOpenSSL server on 127.0.0.1, one thread, closed in teardown. It plays
the hub of ``contracts/hub/protocol.md`` section 10 from the text and records every application byte it receives."""

from __future__ import annotations

import datetime
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from OpenSSL import SSL

from orch.canon.jcs import loads_strict
from orch.crypto import suite
from orch.crypto.encoding import b64u, unb64u
from orch.hubclient.tlslink import client as C
from orch.hubclient.tlslink import framing, tls

Handler = Callable[["TlsHub", dict[str, Any]], Any]


class TlsHub:
    def __init__(self, *, wsk_pub: bytes, on_request: Handler | None = None, max_tls: int | None = None) -> None:
        self.hub_id = os.urandom(16).hex()
        self.workspace_id = os.urandom(16).hex()
        self.wsk_pub = wsk_pub
        self.hk_op = suite.generate_private_key()
        self.hk_op_pub = suite.public_bytes(self.hk_op)
        self.key = suite.generate_private_key()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-hub")])
        now = datetime.datetime.now(datetime.UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(self.key, hashes.SHA256())
        )
        self.spki = tls.spki_sha256(self.cert)
        self.on_request = on_request or (lambda hub, o: hub.reply(o))
        self.exporter_override: bytes | None = None  # make the hub verify auth against another exporter
        self.challenge_hub_id: str | None = None
        self.challenge_delay = 0.0
        self.app_bytes = bytearray()  # every byte received from the client after the handshake
        self.auth_frames: list[dict[str, Any]] = []
        self.auth_verified = threading.Event()
        self.requests: list[dict[str, Any]] = []
        self.handshake_failed = threading.Event()
        self.conn_done = threading.Event()  # the last connection ended (closed by either side)
        self.exporter = b""  # the TLS exporter of the CURRENT connection (every link signature binds it, R1)
        self.h2w_seq = 0  # the h2w counter of the CURRENT connection: replies and pushes share it, first frame is 1
        self._seq_lock = threading.Lock()
        ctx = SSL.Context(SSL.TLS_SERVER_METHOD)
        ctx.set_min_proto_version(SSL.TLS1_2_VERSION if max_tls else SSL.TLS1_3_VERSION)
        ctx.set_max_proto_version(max_tls or SSL.TLS1_3_VERSION)
        ctx.use_privatekey(self.key)
        ctx.use_certificate(self.cert)
        self._ctx = ctx
        self._listen = socket.socket()
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(8)
        self._listen.settimeout(0.1)
        self._stop = threading.Event()
        self._conns: list[tls.TlsConn] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @property
    def port(self) -> int:
        return self._listen.getsockname()[1]

    def start(self) -> TlsHub:
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)
        for c in self._conns:
            c.close()
        self._listen.close()

    # -- building replies ------------------------------------------------------------------------------------------

    def next_seq(self) -> int:
        with self._seq_lock:
            self.h2w_seq += 1
            return self.h2w_seq

    def sign_frame(self, o: dict[str, Any], *, key: Any = None, exporter: bytes | None = None) -> dict[str, Any]:
        """An h2w frame signed over this connection's exporter (or ``exporter``)."""
        sig = suite.sign(key or self.hk_op, C.link_signed_bytes(C.SIG_ROUTE, exporter or self.exporter, o))
        return {"o": o, "sig": b64u(sig)}

    def reply(self, req: dict[str, Any], *, key: Any = None, body: Any = None, **fields: Any) -> dict[str, Any]:
        exporter = fields.pop("exporter", None)
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.link",
            "hv": 0,
            "hub_id": self.hub_id,
            "workspace_id": self.workspace_id,
            "dir": "h2w",
            "seq": self.next_seq(),
            "ts_ms": int(time.time() * 1000),
            "op": req["op"],
            "re": req["seq"],
            "body": {"snapshot": {"stub": 1}} if body is None and req["op"] == "snapshot_get" else (body or {}),
        }
        o.update(fields)
        return self.sign_frame(o, key=key, exporter=exporter)

    # -- the server ------------------------------------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._listen.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._serve(sock)
            except Exception:  # noqa: BLE001  a test hub drops a connection on anything
                pass

    def _serve(self, sock: socket.socket) -> None:
        self.conn_done.clear()
        sock.setblocking(False)
        c = SSL.Connection(self._ctx, sock)
        c.set_accept_state()
        conn = tls.TlsConn(c, sock)
        self._conns.append(conn)
        try:
            conn.handshake(time.monotonic() + 5)
        except Exception:
            self.handshake_failed.set()
            conn.close()
            self.conn_done.set()
            return
        self.exporter = conn.exporter()
        with self._seq_lock:
            self.h2w_seq = 0
        # the pin check happens in the client right after the handshake; record anything it sends afterwards
        try:
            self._stop.wait(self.challenge_delay)
            framing.write_frame(
                conn,
                {
                    "t": "challenge",
                    "hub_id": self.challenge_hub_id or self.hub_id,
                    "challenge": b64u(ch := os.urandom(32)),
                },
                timeout=5,
            )
            a = self._recv(conn, 2.0)
            self.auth_frames.append(a)
            msg = C.link_auth_bytes(self.hub_id, a["workspace_id"], ch, self.exporter_override or conn.exporter())
            if suite.verify(self.wsk_pub, unb64u(a["sig"], 64), msg):
                self.auth_verified.set()
                framing.write_frame(conn, {"t": "ready", "hub_id": self.hub_id}, timeout=5)
            else:
                framing.write_frame(conn, {"t": "error", "error": "auth_failed"}, timeout=5)
                conn.close()
                return
            while not self._stop.is_set():
                req = self._recv(conn, 30)
                self.requests.append(req)
                out = self.on_request(self, req["o"])
                if out is None:
                    self._stop.wait(5)  # silence: the client's read timeout fires
                    continue
                if isinstance(out, bytes):
                    conn.send_all(out, time.monotonic() + 5)
                    continue
                framing.write_frame(conn, out, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()
            self.conn_done.set()

    def _recv(self, conn: tls.TlsConn, timeout: float) -> dict[str, Any]:
        """Read one frame while counting the application bytes."""
        end = time.monotonic() + timeout
        head = conn.recv_exact(4, end)
        self.app_bytes += head
        payload = conn.recv_exact(int.from_bytes(head, "big"), end)
        self.app_bytes += payload
        return loads_strict(payload)
