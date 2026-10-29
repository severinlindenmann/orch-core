"""A contract fake, deliberately independent of the parallel core implementation."""

import json
import socketserver
import tempfile
import threading
import time
import urllib.parse
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
                if (
                    facts["claimed"]
                    or now[0] >= 30
                    or json.loads(raw) != {"code": "demo-not-a-real-code", "port": PORT}
                ):
                    status, out = 403, {"error": {"code": "setup.code_invalid", "message": "expired"}}
                else:
                    facts["claimed"] = True
                    out = {"token": token, "setup_id": "demo-not-a-real-setup"}
            elif facts.get("gone"):
                status, out = 410, {"error": {"code": "setup.ended", "message": "ended"}}
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
                    out = {"passphrase": SECRET, "recovery_words": [SECRET] * 24}
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
    code_path = path.parent / "setup.code"
    code_path.write_text(
        json.dumps(
            {"setup_id": "demo-not-a-real-setup", "code": "demo-not-a-real-code", "expires_at": time.time() + 30}
        )
    )
    code_path.chmod(0o600)
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


def attempt(client):
    """A claim with a fresh, valid browser nonce (the core's answer decides)."""
    nonce = client.app.state.auth.launch(PORT, setup=True)["code"]
    return client.post("/auth/setup", json={"nonce": nonce}, headers={"Origin": ORIGIN})


def claim(client, auth=None):
    auth = auth or client.app.state.auth
    res = client.post("/auth/setup", json={"nonce": auth.launch(PORT, setup=True)["code"]}, headers={"Origin": ORIGIN})
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
    normal = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    assert client.get("/api/setup/state", headers={"X-Setup-Token": normal}).status_code == 401
    assert attempt(client).status_code == 403
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
    client, auth, proxy, facts, _ = setup
    headers = claim(client)
    res = client.post("/api/setup/secrets/reveal", json={}, headers=headers)
    assert res.json()["recovery_words"] == [SECRET] * 24
    assert res.headers["cache-control"] == "no-store"
    assert "set-cookie" not in res.headers
    assert client.post("/api/setup/profile", json={"name": SECRET}, headers=headers).status_code == 200
    assert SECRET not in repr(proxy.__dict__) + repr(auth.__dict__) + caplog.text + str(capsys.readouterr())
    second = client.post("/api/setup/secrets/reveal", json={}, headers=headers)
    assert second.status_code == 410 and second.json()["error"]["code"] == "secrets.shown"
    # "shown once" is an ordinary answer of a live session: the credential survives it (a double reveal from the
    # page must not kill the setup)
    assert auth.valid_setup(headers["X-Setup-Token"])
    facts["gone"] = True
    assert client.get("/api/setup/state", headers=headers).status_code == 410
    assert not auth.valid_setup(headers["X-Setup-Token"])  # the session itself ended: the credential goes


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
    assert attempt(client).status_code == 403


def test_cancel_revokes_and_socket_permissions(setup):
    client, auth, proxy, *_ = setup
    headers = claim(client)
    assert client.post("/api/setup/cancel", json={}, headers=headers).status_code == 200
    assert not auth.valid_setup(headers["X-Setup-Token"])
    (proxy.path.parent / "setup.code").write_text(
        json.dumps(
            {"setup_id": "demo-not-a-real-setup", "code": "demo-not-a-real-code", "expires_at": time.time() + 30}
        )
    )
    (proxy.path.parent / "setup.code").chmod(0o600)
    proxy.path.chmod(0o666)
    assert attempt(client).status_code == 503


def test_agent_refused(setup, monkeypatch):
    client, *_ = setup
    monkeypatch.setenv("ORCH_GRANT", "demo-not-a-real-grant")
    assert attempt(client).status_code == 403


def test_code_file_consumed_and_browser_codes_refused(setup):
    client, _, proxy, *_ = setup
    path = proxy.path.parent / "setup.code"
    assert client.get("/api/setup/state").status_code == 401
    assert (
        client.post("/auth/setup", json={"code": "demo-not-a-real-code"}, headers={"Origin": ORIGIN}).status_code == 400
    )
    assert path.exists()
    claim(client)
    assert not path.exists()
    assert attempt(client).status_code == 403


def test_nonce_required_wrong_reused_expired(setup):
    client, auth, proxy, facts, now = setup
    path = proxy.path.parent / "setup.code"
    post = lambda body: client.post("/auth/setup", json=body, headers={"Origin": ORIGIN})  # noqa: E731
    # no nonce / empty body / wrong / login code / not a string: refused, and the code file is NOT touched
    assert post({}).status_code == 400
    assert post({"nonce": "x" * 43}).status_code == 403
    assert post({"nonce": 7}).status_code == 403
    assert post({"nonce": "x" * 43, "code": "y"}).status_code == 400
    login = auth.launch(PORT)["code"]  # a normal login code is not a setup nonce
    assert post({"nonce": login}).status_code == 403
    assert path.exists() and not facts["claimed"]
    # a setup nonce opens no normal session
    nonce = auth.launch(PORT, setup=True)["code"]
    assert auth.exchange(nonce, "local")[0] is None
    assert client.post("/auth/session", json={"code": nonce}, headers={"Origin": ORIGIN}).status_code == 401
    # correct: bound once; the same nonce again is refused
    res = post({"nonce": nonce})
    assert res.status_code == 200 and auth.valid_setup(res.json()["token"])
    assert post({"nonce": nonce}).status_code == 403
    # an expired nonce is refused too
    late = auth.launch(PORT, setup=True)["code"]
    auth.clock = lambda: 10**10
    assert post({"nonce": late}).status_code == 403


@pytest.mark.parametrize("kind", ["expired", "permissions", "symlink"])
def test_unsafe_or_expired_code_refused(setup, kind):
    client, _, proxy, facts, _ = setup
    path = proxy.path.parent / "setup.code"
    if kind == "expired":
        value = json.loads(path.read_text())
        value["expires_at"] = time.time() - 1
        path.write_text(json.dumps(value))
    elif kind == "permissions":
        path.chmod(0o644)
    else:
        target = path.with_name("other.code")
        path.rename(target)
        path.symlink_to(target)
    assert attempt(client).status_code in (403, 503)
    assert not facts["claimed"]


def test_real_core_claim_and_proxy(tmp_path):
    from orch.setup.server import SetupServer

    class Flow:
        busy = False
        finished = False

        def launch(self, directory):
            self.directory = directory

        def end(self, why):
            pass

        def dispatch(self, method, route, query, body):
            if route == "finish":
                self.finished = True
                return 200, {"url": "/", "workspace_id": "8f1c2d3e-0000-4000-8000-00000000aaaa"}
            return 200, {"route": route, "query": query, "body": body}

    with tempfile.TemporaryDirectory(prefix="b9-core-", dir="/tmp") as directory:
        core = SetupServer(Path(directory) / "setup.sock", Flow())
        core.start()
        try:
            core.launch(str(tmp_path))
            proxy = SetupProxy(core.path)
            app = create_app(PORT, auth=Auth(), setup_proxy=proxy)
            with TestClient(app, base_url=ORIGIN) as client:
                assert client.get("/api/setup/state").status_code == 401
                headers = claim(client)
                assert not core.code_path.exists()
                assert core.ports == frozenset({f"127.0.0.1:{PORT}", f"localhost:{PORT}"})
                res = client.post("/api/setup/profile", json={"agent_grant": True}, headers=headers)
                assert res.json()["body"] == {"agent_grant": True}
                res = client.get("/api/setup/progress?run_id=demo-run", headers=headers)
                assert res.json()["query"] == {"run_id": "demo-run"}
                assert res.headers["cache-control"] == "no-store"
                done = client.post("/api/setup/finish", json={}, headers=headers).json()
                url = done["url"]
                # F1: the new workspace's id reaches the page, so it can select it before the dashboard opens
                assert done["workspace_id"] == "8f1c2d3e-0000-4000-8000-00000000aaaa"
                login = urllib.parse.parse_qs(urllib.parse.urlsplit(url).fragment)
                assert url.startswith("/#launch=") and login["to"] == ["/"]
                # the code opens a normal session once; the setup scope is gone
                normal = client.post(
                    "/auth/session", json={"code": login["launch"][0]}, headers={"Origin": ORIGIN}
                ).json()["token"]
                assert app.state.auth.valid(normal) and not app.state.auth.valid_setup(normal)
                assert (
                    client.post(
                        "/auth/session", json={"code": login["launch"][0]}, headers={"Origin": ORIGIN}
                    ).status_code
                    == 401
                )
                assert client.get("/api/setup/state", headers=headers).status_code == 401
        finally:
            core.stop()


SERVICE_URL = "http://192.168.64.4:8151/#launch=" + "a" * 43 + "&to=%2F"


def test_real_core_finish_keeps_service_login_url(tmp_path):
    from orch.setup.server import SetupServer

    class Flow:
        busy = False
        finished = False

        def launch(self, directory):
            pass

        def end(self, why):
            pass

        def dispatch(self, method, route, query, body):
            return 200, {"url": SERVICE_URL, "workspace_id": "8f1c2d3e-0000-4000-8000-00000000aaaa"}

    with tempfile.TemporaryDirectory(prefix="b9-core-", dir="/tmp") as directory:
        core = SetupServer(Path(directory) / "setup.sock", Flow())
        core.start()
        try:
            core.launch(str(tmp_path))
            app = create_app(PORT, auth=Auth(), setup_proxy=SetupProxy(core.path))
            with TestClient(app, base_url=ORIGIN) as client:
                headers = claim(client)
                done = client.post("/api/setup/finish", json={}, headers=headers).json()
                assert done.get("url") == SERVICE_URL, done
        finally:
            core.stop()
