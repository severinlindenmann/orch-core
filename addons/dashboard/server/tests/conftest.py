import json
import os
import socket
import threading
from pathlib import Path

import pytest
from devhost.host import StubHost
from fastapi.testclient import TestClient

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.gateway import Gateway
from orch_dashboard.hostlink import HostLink

PORT = 43210
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def p1(tmp_path, monkeypatch):
    # Use core's signed-store test builder, not a dashboard mock model.
    from orch.addons.install import grant_event, package_dir
    from orch.addons.manifest import load_manifest
    from orch.addons.package import read_package

    from tests.store.helpers import Env

    env = Env(tmp_path)
    env.bootstrap()
    env.new_ticket("Real P1 ticket")
    manifest = Path(__file__).parents[2] / "orch-addon.json"
    directory = package_dir(env.root, "dashboard")
    directory.mkdir(parents=True)
    (directory / "orch-addon.json").write_bytes(manifest.read_bytes())
    event = grant_event(load_manifest(manifest.read_bytes()), read_package(directory))
    typ = event.pop("type")
    env.store.append(env.person_event(env.owner, "workspace", typ, **event), log="workspace")
    (env.root / ".orch-dev").touch()
    monkeypatch.setenv("ORCH_DEV", "1")
    yield env
    env.store.close()


@pytest.fixture
def stub(p1):
    return StubHost(p1.root, p1.host_state, p1.owner.ref)


@pytest.fixture
def gateway(stub, p1, monkeypatch):
    directory = p1.host_state / "host"
    directory.mkdir(mode=0o700)
    path = directory / "workspaces.json"
    path.write_text(
        json.dumps(
            {
                "workspaces": [
                    {
                        "workspace_id": stub.ws,
                        "host_uid": os.getuid(),
                        "peers": {str(os.getuid()): {"class": "person", "person": stub.person}},
                    }
                ]
            }
        )
    )
    path.chmod(0o600)
    threads = []

    def connect(path, uid, ws):
        client, host = socket.socketpair()
        thread = threading.Thread(target=stub.serve_connection, args=(host, ws is None), daemon=True)
        thread.start()
        threads.append(thread)
        return HostLink(client, uid, ws)

    monkeypatch.setattr(HostLink, "connect", connect)
    gateway = Gateway(p1.host_state)
    yield gateway
    gateway.close()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.fixture
def auth():
    return Auth()


@pytest.fixture
def client(gateway, auth, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<html><script src="/assets/main-hash.js"></script></html>')
    (static / "assets/main-hash.js").write_text('console.log("public bundle")')
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def token(client, auth):
    launch = auth.launch(PORT)
    res = client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN})
    assert res.status_code == 200
    return res.json()["token"]
