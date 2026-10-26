"""M7a test kit: core's in-process host and runner (the helpers of core's H11b tests) behind the dashboard's own app.

The host runs in a thread of this process (`tests.host.live_helpers.world`) with the `runner_identity` hook core's tests
use (the runner is this process); the runner is core's `Runner` helper (supervisor, term socket, host link) with
`$SHELL=/bin/sh`. Everything lives under pytest's tmp_path and core's short run directory: no launchd, no keychain,
no real `~/.config/orch`.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import struct
import time
from pathlib import Path

from fastapi.testclient import TestClient

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.gateway import Gateway

PORT = 43210
ORIGIN = f"http://127.0.0.1:{PORT}"
WS_HEADERS = {"origin": ORIGIN, "host": f"127.0.0.1:{PORT}"}


def grant(w, name, capabilities):
    w.person_event(
        "addon.granted", "workspace", name=name, version="0.1.0", package_sha256="sha256:" + "0" * 64,
        capabilities=capabilities, fields={}, sections=[], artifact_kinds=[],
    )  # fmt: skip


def grant_dashboard(w):
    """The dashboard's real package and its grant (the server refuses a workspace without an active dashboard)."""
    from orch.addons.install import grant_event, package_dir
    from orch.addons.manifest import load_manifest
    from orch.addons.package import read_package

    manifest = Path(__file__).parents[2] / "orch-addon.json"
    directory = package_dir(Path(w.lab.ws.store_dir), "dashboard")
    directory.mkdir(parents=True)
    (directory / "orch-addon.json").write_bytes(manifest.read_bytes())
    event = grant_event(load_manifest(manifest.read_bytes()), read_package(directory))
    typ = event.pop("type")
    w.person_event(typ, "workspace", **event)


class TermWorld:
    """`w` (core's world), `runner`, the dashboard `client` with a bearer `token`, and helpers for the routes."""

    def __init__(self, tmp_path: Path, monkeypatch, *, terminals_addon: bool = True):
        from orch.runner import enablement

        from tests.host.lifecycle_helpers import UID
        from tests.host.live_helpers import world
        from tests.host.test_runner_socket import Runner

        monkeypatch.setenv("SHELL", "/bin/sh")
        monkeypatch.setenv("PS1", "$ ")
        self.w = w = world(tmp_path, runner_identity=lambda cred: cred.pid == os.getpid())
        self.runner = None
        try:
            grant_dashboard(w)
            if terminals_addon:
                grant(w, "terminals", ["pty"])
            enablement.set_enabled(Path(w.lab.state), w.wid, True, UID, terminals=True, text="t")
            rstate = tmp_path / "runner-state"
            rstate.mkdir(mode=0o700)
            self.runner = Runner(w, rstate)
            monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(w.lab.run))
            monkeypatch.delenv("ORCH_HOST_SOCKET", raising=False)
            self.gateway = Gateway(Path(w.lab.state))
            self.auth = Auth()
            static = tmp_path / "static"
            (static / "assets").mkdir(parents=True)
            (static / "index.html").write_text("<html><head></head></html>")
            self.app = create_app(PORT, self.gateway, self.auth, static)
            self.client = TestClient(self.app, base_url=ORIGIN)
            self.client.__enter__()
            launch = self.auth.launch(PORT)
            res = self.client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN})
            assert res.status_code == 200
            self.token = res.json()["token"]
        except BaseException:
            self.close()
            raise

    @property
    def ws(self) -> str:
        return self.w.wid

    def headers(self, token: str | None = None) -> dict:
        return {"Authorization": f"Bearer {token or self.token}", "Origin": ORIGIN, "Content-Type": "application/json"}

    def get(self, path: str, **kw):
        return self.client.get(f"/api/w/{self.ws}{path}", headers=self.headers(kw.pop("token", None)), **kw)

    def post(self, path: str, body: dict | None = None, **kw):
        return self.client.post(
            f"/api/w/{self.ws}{path}", content=json.dumps(body or {}), headers=self.headers(kw.pop("token", None)), **kw
        )

    def open_shell(self, **args) -> dict:
        from orch.host.control_client import new_idem

        pre = self.post("/terminals/preview", args)
        assert pre.status_code == 200, pre.text
        assert pre.json()["blocked"] is None, pre.json()["blocked"]
        res = self.post("/terminals/open", {**args, "spec": pre.json()["spec"], "idem": new_idem()})
        assert res.status_code == 200, res.text
        return res.json()["run"]

    def ticket(self, run: str, **body) -> str:
        res = self.post(f"/runs/{run}/attach", body)
        assert res.status_code == 200, res.text
        return res.json()["ticket"]

    @contextlib.contextmanager
    def connect(self, run: str, **body):
        """A WebSocket on `/term` with a fresh ticket sent; the caller reads `ready` (or the close). Leaving closes it
        as a browser tab would and waits until the server has finished with it."""
        ticket = self.ticket(run, **body)
        with self.client.websocket_connect("/term", headers=WS_HEADERS) as ws:
            ws.send_text(json.dumps({"ticket": ticket}))
            try:
                yield ws
            finally:
                settle(ws)

    def close(self) -> None:
        if getattr(self, "client", None) is not None:
            self.client.__exit__(None, None, None)
        if getattr(self, "gateway", None) is not None:
            self.gateway.close()
        if self.runner is not None:
            self.runner.close()
        self.w.h.stop()
        self.w.lab.close()


def receive(ws, timeout: float = 15.0) -> dict:
    """The next message from the server, or AssertionError after `timeout` (never blocks a test forever)."""
    import anyio

    deadline = time.monotonic() + timeout
    while True:
        try:
            msg = ws.portal.call(ws._send_rx.receive_nowait)
        except anyio.WouldBlock:
            assert time.monotonic() < deadline, "no message from the server"
            time.sleep(0.005)
            continue
        if msg.get("type") == "websocket.close":
            ws.closed_by_server = msg.get("code")
        return msg


def text(ws) -> str:
    msg = receive(ws)
    assert msg.get("text") is not None, msg.get("type")
    return msg["text"]


def settle(ws) -> None:
    """Disconnect (unless the server already closed) and wait for the server's close, so the app has finished."""
    if getattr(ws, "closed_by_server", None) is not None:
        return
    with contextlib.suppress(Exception):
        ws.close(1000)
    with contextlib.suppress(AssertionError):
        while receive(ws, 10).get("type") != "websocket.close":
            pass


def read_until(ws, marker: bytes, timeout: float = 15.0) -> tuple[bytes, list[dict]]:
    """Output bytes (offsets checked to be consecutive) until `marker`, and the text messages seen on the way."""
    got, texts, expect = b"", [], None
    deadline = time.monotonic() + timeout
    while marker not in got:
        assert time.monotonic() < deadline, "marker not seen"
        msg = receive(ws)
        if msg.get("type") == "websocket.close":
            raise AssertionError(f"closed {msg.get('code')}")
        if msg.get("bytes") is not None:
            raw = msg["bytes"]
            off = struct.unpack(">Q", raw[:8])[0]
            assert expect is None or off == expect
            expect = off + len(raw) - 8
            got += raw[8:]
        else:
            texts.append(json.loads(msg["text"]))
    return got, texts


def until_text(ws, kind: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        assert time.monotonic() < deadline, f"no {kind}"
        msg = receive(ws)
        if msg.get("type") == "websocket.close":
            return {"t": "_close", "code": msg.get("code")}
        if msg.get("text") is not None:
            value = json.loads(msg["text"])
            if value.get("t") == kind:
                return value


def until_close(ws, timeout: float = 15.0) -> tuple[int, list[dict]]:
    texts = []
    deadline = time.monotonic() + timeout
    while True:
        assert time.monotonic() < deadline, "no close"
        msg = receive(ws)
        if msg.get("type") == "websocket.close":
            return msg.get("code"), texts
        if msg.get("text") is not None:
            texts.append(json.loads(msg["text"]))


def key(data: bytes) -> bytes:
    return b"\x00" + data


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
