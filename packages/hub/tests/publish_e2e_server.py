"""Runs a REAL orch-publish app (uvicorn on 127.0.0.1, ephemeral port) for the D11 e2e test.

Started by tests/test_publish_e2e.py with `uv run --project packages/publish` and cwd = packages/publish.
It registers a workspace through orch-publish's test helpers (vector keys), prints
``PORT <n>``, then serves HTTP (including orch-publish's /v2/hub/ admin endpoints) until stdin closes.
"""

import socket
import sys
import tempfile
import threading

import uvicorn

sys.path.insert(0, ".")
from pubsrv.app import create_app
from tests.test_publish_api_auth import NOW, WS_A, Env


def main() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    host = f"127.0.0.1:{port}"
    tmp = tempfile.mkdtemp(prefix="d11-pub-")
    e = Env.__new__(Env)
    e.data, e.now = tmp + "/data", NOW
    e.app = create_app(e.data, host, clock=lambda: NOW, dev_mode=True)
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
    from fastapi.testclient import TestClient
    from tests.test_publish_api_auth import _priv

    e.c, e.wsk, e.path = TestClient(e.app, base_url="http://" + host), _priv("wsk_a.sig"), e.app.state.settings.db_path
    e.allow()
    assert e.register().status_code == 200  # the workspace card as the hub hands it over (V3-D36)
    srv = uvicorn.Server(uvicorn.Config(e.app, log_level="warning"))
    threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True).start()
    while not srv.started:
        threading.Event().wait(0.02)
    print(f"PORT {port} WS {WS_A} NOW {NOW}", flush=True)
    sys.stdin.read()  # lives until the test closes stdin
    srv.should_exit = True


main()
