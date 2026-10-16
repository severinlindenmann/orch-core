"""A contract fake, deliberately independent of the parallel core implementation."""

import json
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orch_dashboard.adapters.setup import SetupProxy
from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth

PORT = 43210
ORIGIN = f"http://127.0.0.1:{PORT}"
SECRET = "demo-not-a-real-recovery"


@pytest.fixture
def setup(tmp_path):
    now = [0]
    facts = {"claimed": False, "revealed": False, "last": 0, "busy": False, "ended": False}
    token = "demo-not-a-real-setup-token"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def go(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status, out = 200, {"ok": True}
            if self.path == "/claim":
                if facts["claimed"] or now[0] >= 30 or json.loads(raw) != {"code": "demo-not-a-real-code"}:
                    status, out = 403, {"error": {"code": "setup.code_invalid", "message": "expired"}}
                else:
                    facts["claimed"] = True
                    out = {"token": token, "setup_id": "demo-not-a-real-setup"}
            elif (
                facts["ended"]
                or self.headers.get("X-Setup-Token") != token
                or (not facts["busy"] and now[0] - facts["last"] >= 600)
            ):
                status, out = 401, {"error": {"code": "setup.token", "message": "expired"}}
            elif self.path == "/api/setup/secrets/reveal":
                if facts["revealed"]:
                    status, out = 410, {"error": {"code": "secrets.shown", "message": "already shown"}}
                else:
                    facts["revealed"] = True
                    out = {"passphrase": SECRET, "recovery_words": [SECRET] * 24, "check_positions": [8, 17, 23]}
            elif self.path == "/api/setup/profile":
                assert json.loads(raw)["name"] == SECRET
            elif self.path == "/api/setup/state":
                out = {"phase": "profile", "mode": "new", "dir": "/demo", "dir_name": "demo"}
            elif self.path == "/api/setup/create":
                facts["busy"] = True
                out = {"run_id": "demo-run"}
            elif self.path in ("/api/setup/finish", "/api/setup/cancel"):
                facts["ended"] = True
            facts["last"] = now[0]
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = go

    short = tempfile.TemporaryDirectory(prefix="b9-", dir="/tmp")
    path = Path(short.name) / "setup.sock"
    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    auth = Auth()
    (tmp_path / "index.html").write_text("<head></head><main>setup shell</main>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/setup.js").write_text("// setup")
    proxy = SetupProxy(path)
    app = create_app(PORT, auth=auth, static=tmp_path, setup_proxy=proxy)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, auth, proxy, facts, now
    server.shutdown()
    server.server_close()
    thread.join()
    short.cleanup()


def claim(client):
    res = client.post("/auth/setup", json={"code": "demo-not-a-real-code"}, headers={"Origin": ORIGIN})
    assert res.status_code == 200
    return {"X-Setup-Token": res.json()["token"], "Origin": ORIGIN}


def test_scope_claim_and_no_workspace(setup):
    client, auth, _, _, _ = setup
    assert client.get("/setup").status_code == 200
    headers = claim(client)
    token = headers["X-Setup-Token"]
    assert not auth.valid(token) and auth.valid_setup(token)
    assert token not in repr(auth.__dict__)
    assert client.get("/api/workspaces", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    normal = auth.exchange(auth.launch(PORT)["code"])[0]["token"]
    assert client.get("/api/setup/state", headers={"X-Setup-Token": normal}).status_code == 401
    assert (
        client.post("/auth/setup", json={"code": "demo-not-a-real-code"}, headers={"Origin": ORIGIN}).status_code == 403
    )
    assert client.get("/api/setup/state", headers=headers).json()["phase"] == "profile"
    for path in ("launch", "claim", "shutdown", "../shutdown"):
        assert client.post("/api/setup/" + path, json={}, headers=headers).status_code in (401, 404)


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Host": "evil.test:43210"}, 421),
        ({"Host": "localhost:43211"}, 421),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://localhost:43210"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
    ],
)
def test_setup_host_origin(setup, headers, status):
    client, *_ = setup
    valid = claim(client)
    assert client.post("/api/setup/profile", json={}, headers={**valid, **headers}).status_code == status


def test_localhost_duplicates_csrf_and_size(setup):
    client, *_ = setup
    headers = claim(client)
    local = {**headers, "Host": f"localhost:{PORT}", "Origin": f"http://localhost:{PORT}"}
    assert client.get("/api/setup/state", headers=local).status_code == 200
    assert client.get("/assets/setup.js", headers={"Host": f"localhost:{PORT}"}).status_code == 200
    assert (
        client.post("/api/setup/profile", json={}, headers={"X-Setup-Token": headers["X-Setup-Token"]}).status_code
        == 403
    )
    assert (
        client.get("/api/setup/state", headers=[*(headers.items()), ("X-Setup-Token", "duplicate")]).status_code == 401
    )
    assert (
        client.post(
            "/api/setup/profile", content=b"x" * 65537, headers={**headers, "Content-Type": "application/json"}
        ).status_code
        == 400
    )


def test_no_retention_logging_or_cache_one_shot(setup, caplog, capsys):
    client, auth, proxy, *_ = setup
    headers = claim(client)
    res = client.post("/api/setup/secrets/reveal", json={}, headers=headers)
    assert res.json()["recovery_words"] == [SECRET] * 24
    assert res.headers["cache-control"] == "no-store"
    assert "set-cookie" not in res.headers
    assert client.post("/api/setup/profile", json={"name": SECRET}, headers=headers).status_code == 200
    assert SECRET not in repr(proxy.__dict__) + repr(auth.__dict__) + caplog.text + str(capsys.readouterr())
    assert client.post("/api/setup/secrets/reveal", json={}, headers=headers).status_code == 410
    assert not auth.valid_setup(headers["X-Setup-Token"])


def test_launch_expiry_idle_and_run_exception(setup):
    client, _, _, facts, now = setup
    headers = claim(client)
    now[0] = 601
    facts["busy"] = True
    assert client.get("/api/setup/state", headers=headers).status_code == 200
    facts["busy"] = False
    now[0] += 600
    assert client.get("/api/setup/state", headers=headers).status_code == 401


def test_code_ttl_and_cancel(setup):
    client, _, _, _, now = setup
    now[0] = 30
    assert (
        client.post("/auth/setup", json={"code": "demo-not-a-real-code"}, headers={"Origin": ORIGIN}).status_code == 403
    )


def test_cancel_revokes_and_socket_permissions(setup):
    client, auth, proxy, *_ = setup
    headers = claim(client)
    assert client.post("/api/setup/cancel", json={}, headers=headers).status_code == 200
    assert not auth.valid_setup(headers["X-Setup-Token"])
    proxy.path.chmod(0o666)
    assert (
        client.post("/auth/setup", json={"code": "demo-not-a-real-code"}, headers={"Origin": ORIGIN}).status_code == 503
    )


def test_agent_refused(setup, monkeypatch):
    client, *_ = setup
    monkeypatch.setenv("ORCH_GRANT", "demo-not-a-real-grant")
    assert (
        client.post("/auth/setup", json={"code": "demo-not-a-real-code"}, headers={"Origin": ORIGIN}).status_code == 403
    )
