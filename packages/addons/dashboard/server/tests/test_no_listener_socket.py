"""R3 T4 (D89): no code path of a remote stream opens a socket to a listener. The adapter answers in-process; the only
sockets of a stream's life are the host connection the dashboard already has (a socketpair here)."""

# ruff: noqa: F811

import asyncio
import json
import socket
import time
from pathlib import Path

from adapter_kit import DEVICE, PERSON, STREAM, head
from conftest import PORT
from device_kit import app, click_start, fake, passkey_store, ws  # noqa: F401
from orch.crypto.encoding import b64u, unb64u
from test_remote_device_adapter import FakeLink

from orch_dashboard import remote_device as rd
from orch_dashboard.remote_device import END, HEAD, frame


def test_no_code_path_opens_a_socket_to_a_listener(app, auth, ws, gateway, monkeypatch, tmp_path):
    connects = []
    real_connect = socket.socket.connect

    def spy_connect(self, address):
        connects.append(("connect", address))
        return real_connect(self, address)

    real_create = socket.create_connection

    def spy_create(*a, **kw):
        connects.append(("create_connection", a[:1]))
        return real_create(*a, **kw)

    async def spy_open(*a, **kw):
        connects.append(("open_connection", a[:2]))
        raise AssertionError("a stream must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket, "create_connection", spy_create)
    monkeypatch.setattr(asyncio, "open_connection", spy_open)
    monkeypatch.setattr(asyncio, "open_unix_connection", spy_open)

    async def main():
        loop = asyncio.get_running_loop()
        for name in ("create_connection", "create_unix_connection", "sock_connect"):

            async def spy(*a, _n=name, **kw):
                connects.append((_n, a[:2]))
                raise AssertionError("a stream must not open a connection")

            monkeypatch.setattr(loop, name, spy)
        link = FakeLink()
        term = rd.Terminator(app, auth, loop)
        att = rd.Attachment(ws, term, lambda w: link, wait_ms=100, active_ms=20)
        att.start()
        try:
            ident = {"workspace_id": ws, "device_id": DEVICE, "person": PERSON, "level": "work"}
            link.push({"t": "open", "stream_id": STREAM, "api": 1, **ident})
            for ident_id, path in ((1, "/api/workspaces"), (2, f"/api/w/{ws}/today")):
                for kind, payload in ((HEAD, head("GET", path)), (END, b"")):
                    msg = {"t": "frame", "stream_id": STREAM, "bytes": b64u(frame(kind, ident_id, payload))}
                    link.push({**msg, "identity": ident} if kind == HEAD else msg)
            end = time.monotonic() + 8
            while time.monotonic() < end:
                with link.mu:
                    ends = [s for s in link.sent if unb64u(s["bytes"])[0] == END]
                if len(ends) == 2:
                    break
                await asyncio.sleep(0.02)
            assert len(ends) == 2, "the round trip did not finish"
            with link.mu:
                bodies = b"".join(unb64u(s["bytes"])[5:] for s in link.sent if unb64u(s["bytes"])[0] == 2)
            assert ws.encode() in bodies  # the host (stub) was asked and the answer came back in-process
            link.push({"t": "closed", "stream_id": STREAM, "reason": "revoked"})
            await asyncio.sleep(0.1)
        finally:
            att.stop()
            att.join(3)

    asyncio.run(main())

    # every address a listener of this server has: the loopback port, the net listener, the front socket, ctl.sock
    listeners = {
        ("127.0.0.1", PORT),
        ("0.0.0.0", PORT),
        str(gateway.state / "dashboard" / "front.sock"),
        str(gateway.state / "dashboard" / "ctl.sock"),
    }
    for kind, address in connects:
        assert address not in listeners and PORT not in json.dumps(address, default=str), (kind, address)
    assert connects == [], connects


def test_the_adapter_module_has_no_connect_call():
    text = Path(rd.__file__).read_text()
    for needle in (".connect(", "create_connection", "open_connection", "open_unix_connection", "sock_connect"):
        assert needle not in text, needle
