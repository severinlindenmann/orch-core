"""Shared pieces of the remote client's loopback tests: a static build in tmp_path, a fake end-to-end session that
builds
its request head with the REAL ``orch.remoteclient.frames`` (so the header allow-list is the product's), and a tiny
HTTP/WebSocket driver that can send any Host / Origin."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orch.remoteclient import Response
from orch.remoteclient import frames as F

WS_A = "a" * 32
WS_B = "b" * 32


def make_static(root: Path) -> Path:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<html><head><script src="/assets/dash.js"></script></head>DASHBOARD</html>')
    (root / "remote-client.html").write_text('<html><head><script src="/assets/rc.js"></script></head>REMOTE</html>')
    (root / "assets/dash.js").write_text("console.log('dashboard')")
    (root / "assets/rc.js").write_text("console.log('client')")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    return root


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self._q: list[tuple[str, Any]] = []
        self._cv = threading.Condition()
        self.closed = False

    def send_text(self, text: str) -> None:
        self.sent.append(("text", text))
        with self._cv:
            self._q.append(("text", "echo:" + text))
            self._cv.notify_all()

    def send_binary(self, data: bytes) -> None:
        self.sent.append(("binary", data))

    def close(self, code: int = 1000) -> None:
        self.closed = True

    def recv(self, timeout: float = 60.0):
        with self._cv:
            if not self._q:
                self._cv.wait(timeout)
            return self._q.pop(0) if self._q else None


class FakeSession:
    """What the loopback server sees of ``orch.remoteclient.Session``."""

    def __init__(self, ws: str = WS_A, name: str | None = "Billing") -> None:
        self.workspace_id, self.name, self.closed = ws, name, None
        self.requests: list[dict[str, Any]] = []
        self.ws_opens: list[dict[str, Any]] = []
        self.answer: tuple[int, list[tuple[str, str]], list[bytes]] = (
            200,
            [("content-type", "application/json")],
            [b'{"ok":true}'],
        )
        self.ws = FakeWs()

    def request(self, method, path, query="", headers=(), body=b""):
        head = json.loads(F.build_request_head(method, path, query, headers))  # the product's own allow-list
        self.requests.append({"head": head, "body": bytes(body)})
        status, hs, chunks = self.answer
        chunks = list(chunks)
        return Response(
            status, F.clean_response_headers(hs), lambda timeout: chunks.pop(0) if chunks else None, lambda: None
        )

    def websocket(self, path, query="", headers=()):
        self.ws_opens.append(json.loads(F.build_request_head(None, path, query, headers)))
        return self.ws

    def close(self) -> None:
        self.closed = "closed"


def raw(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    host: str | None = "default",
    timeout: float = 10,
) -> tuple[int, dict[str, str], bytes]:
    """One request with exactly the headers given (``host=None``: no Host header at all)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            c.putheader("Host", f"127.0.0.1:{port}" if host == "default" else host)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def fake_client(*, hub_name: str = "Hub Name", own_name: str | None = None) -> SimpleNamespace:
    """A stand-in for ``orch.remoteclient.Client`` with the verbs the controller calls."""
    calls: list[tuple[str, tuple, dict]] = []
    store = SimpleNamespace(
        hub=lambda: {"origin": "https://hub.example:8443", "link": "hub.example:8444", "hub_id": "f" * 32, "spki": "x"},
        workspaces=lambda: {WS_A: {"name": own_name, "device_id": "rd_" + "1" * 32}} if own_name else {WS_A: {}},
    )
    c = SimpleNamespace(store=store, calls=calls, sessions={}, hub_name=hub_name)
    c.signed_in = lambda: True
    c.workspaces = lambda: [
        {
            "workspace_id": WS_A,
            "online": True,
            "added": True,
            "name": own_name or hub_name,
            "name_from_hub": own_name is None,
            "hub_name": hub_name,
        }
    ]

    def open_(ws, notify=None):
        calls.append(("open", (ws,), {}))
        s = c.sessions.setdefault(ws, FakeSession(ws, own_name))
        return s

    c.open = open_
    return c
