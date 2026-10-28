"""Shared pieces of the R1-4 tests: names, a fake clock, an echo app, and the fixtures of one app with two listeners."""

# ruff: noqa: F811

import dataclasses
import shutil
import tempfile
from pathlib import Path

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient
from orch.click.passkey_store import PasskeyStore

from orch_dashboard.app import create_app
from orch_dashboard.front import Front
from orch_dashboard.policy import remote_policy

NAME = "orch.example.com"


HTTPS = f"https://{NAME}"


SOURCE = "203.0.113.7"


RUN = "r_" + "0" * 26


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def echo(seen):
    async def app(scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        seen.append(scope)
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


@pytest.fixture
def socket_dir():
    path = tempfile.mkdtemp(prefix="r14")  # short: a Unix socket path is limited to ~104 bytes
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def passkey_store(tmp_path):
    """The person's passkey store in a private temporary directory (never the real state directory)."""
    directory = tmp_path / "passkeys"
    directory.mkdir(mode=0o700)
    return PasskeyStore(directory / "passkeys.json")


@pytest.fixture
def app(gateway, auth, tmp_path, passkey_store):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<html><head><script src="/assets/main-hash.js"></script></head></html>')
    (static / "assets/main-hash.js").write_text("x")
    return create_app(PORT, gateway, auth, static, remote=remote_policy(NAME), passkeys=passkey_store)


@pytest.fixture
def ungated(gateway, auth, tmp_path, passkey_store):
    """The front with the route table switched off: tests of the handlers' own refusals (defence in depth, R1-4), which
    the gate would otherwise answer first."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    policy = dataclasses.replace(remote_policy(NAME), route_gate=False)
    app = create_app(PORT, gateway, auth, static, remote=policy, passkeys=passkey_store)
    return TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE, "X-Orch-Click-Contract": "4"})


@pytest.fixture
def loop(app):
    return TestClient(app, base_url=ORIGIN, headers={"X-Orch-Click-Contract": "4"})


@pytest.fixture
def front(app):
    return TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE, "X-Orch-Click-Contract": "4"})


@pytest.fixture
def local(auth):
    return auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]


@pytest.fixture
def remote(auth):
    return auth.open_remote()["token"]


def bearer(token, origin=None):
    return {"Authorization": f"Bearer {token}", **({"Origin": origin} if origin else {})}


def terminals_app(gateway, auth, tmp_path, on):
    store = PasskeyStore(tmp_path / "passkeys.json")
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    policy = dataclasses.replace(remote_policy(NAME), terminals=on)
    return create_app(PORT, gateway, auth, static, remote=policy, passkeys=store)
