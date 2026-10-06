"""#167: the switcher lists only dashboards that prove who they are (GET /__orch/status), probes off the page render,
prunes stale entries, and stays off until the human turns it on."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orch.dashboard import switcher

pytest.importorskip("fastapi")

WID = "northwind-id-0123456789"
LOCAL = "http://127.0.0.1:8765"


# -- the endpoint ---------------------------------------------------------------------------------------------------

def _app(ws, **kw):
    from orch.dashboard.app import create_app
    return create_app(ws, "tok", **kw)


def _local(app):
    from fastapi.testclient import TestClient
    return TestClient(app, base_url=LOCAL, client=("127.0.0.1", 50000))


def test_status_answers_locally_without_a_token(ws, put):
    put("testing", sections={"Verification": "ok"})
    switcher.write_needs(ws, 1)
    r = _local(_app(ws)).get("/__orch/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    from orch import __version__
    assert body["service"] == "orch-core-dashboard" and body["schema"] == 1 and body["orch_version"] == __version__
    assert body["workspace_id"] == switcher.workspace_id(ws) and body["name"] == "acme" and body["pid"] == os.getpid()
    assert body["started"] and body["addons"] == [] and body["needs"] == 1
    assert set(body) == {"service", "schema", "orch_version", "workspace_id", "name", "pid", "started", "addons", "needs"}


def test_status_lists_enabled_addons_with_versions(ws):
    from orch.addons import userfiles
    userfiles.set_enabled(ws.root, "graph", True)
    app = _app(ws)
    app.state.addons.reload()
    assert _local(app).get("/__orch/status").json()["addons"] == [{"name": "graph", "version": "0.1.0"}]


@pytest.mark.parametrize("base_url,client", [
    ("http://192.168.1.5:8765", ("192.168.1.9", 50000)),  # the LAN option: another machine
    ("http://evil.example:8765", ("127.0.0.1", 50000)),  # DNS rebinding: loopback client, foreign Host
])
def test_status_is_loopback_only(ws, base_url, client):
    from fastapi.testclient import TestClient
    r = TestClient(_app(ws), base_url=base_url, client=client).get("/__orch/status")
    assert r.status_code == 404 and "workspace_id" not in r.text


def test_status_is_never_remote(ws):
    from orch.dashboard import remote_gate
    assert remote_gate.TAGS[("GET", "/__orch/status")].scope is remote_gate.NEVER


def test_only_status_skips_the_token(ws):
    c = _local(_app(ws))
    assert c.get("/__orch/status/").status_code == 401
    assert c.post("/__orch/status").status_code == 401
    assert c.get("/").status_code == 401


# -- the probe against real loopback servers -------------------------------------------------------------------------

def _serve(body: bytes | None = None, *, status=200, delay=0.0):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def servers():
    started = []

    def make(*a, **kw):
        s = _serve(*a, **kw)
        started.append(s)
        return s.server_address[1]

    yield make
    for s in started:
        s.shutdown()
        s.server_close()


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_verdicts(servers):
    ok = servers(json.dumps({"service": switcher.SERVICE, "workspace_id": WID}).encode())
    assert switcher.probe(ok)["workspace_id"] == WID
    assert switcher.probe(servers(b"<html>docs preview</html>")) is switcher.GONE
    assert switcher.probe(servers(b"[]")) is switcher.GONE
    assert switcher.probe(servers(b"{}", status=401)) is switcher.GONE
    assert switcher.probe(servers(b"{" + b" " * 10000 + b"}")) is switcher.GONE  # too large
    assert switcher.probe(_free_port()) is switcher.GONE  # nothing listens
    assert switcher.probe(servers(b"{}", delay=1.0), timeout=0.1) is None  # busy, not stale


def _register_other(tmp_path, name, port, *, pid=None, wid=WID, **extra):
    from orch.dashboard.launch import config_dir
    path = config_dir() / "workspaces.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    entry = {"path": str(folder), "name": name, "last_port": port, "workspace_id": wid, **extra}
    if pid is not None:
        entry["pid"] = pid
    data[str(folder)] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return str(folder)


def _entries():
    from orch.dashboard.launch import config_dir
    return json.loads((config_dir() / "workspaces.json").read_text())


def test_scan_lists_only_proven_dashboards_and_prunes_the_rest(ws, tmp_path, servers):
    me = os.getpid()  # alive: the pid check passes, the probe decides
    good = servers(json.dumps({"service": switcher.SERVICE, "workspace_id": WID, "needs": 2}).encode())
    foreign = servers(json.dumps({"service": "docs-preview", "workspace_id": WID}).encode())
    other_ws = servers(json.dumps({"service": switcher.SERVICE, "workspace_id": "someone-else-0123456"}).encode())
    slow = servers(json.dumps({"service": switcher.SERVICE, "workspace_id": WID}).encode(), delay=1.0)
    switcher.register(ws, 8765)
    keys = {
        "good": _register_other(tmp_path, "good", good, pid=me),
        "foreign": _register_other(tmp_path, "foreign", foreign, pid=me),
        "other_ws": _register_other(tmp_path, "other_ws", other_ws, pid=me),
        "refused": _register_other(tmp_path, "refused", _free_port(), pid=me),
        "dead": _register_other(tmp_path, "dead", good, pid=2 ** 22 + 12345, addons={"wiki": {"enabled": True}}),
        "nopid": _register_other(tmp_path, "nopid", good),
        "slow": _register_other(tmp_path, "slow", slow, pid=me),
    }
    assert switcher.scan(ws) == [{"name": "good", "url": f"http://127.0.0.1:{good}/", "needs": 2}]
    data = _entries()
    for name in ("foreign", "other_ws", "refused", "dead"):
        assert "pid" not in data[keys[name]], name
    assert data[keys["dead"]]["addons"] == {"wiki": {"enabled": True}}  # the entry and its settings stay
    assert data[keys["good"]]["pid"] == me and data[keys["slow"]]["pid"] == me  # a timeout is not proof
    assert "pid" not in data[keys["nopid"]] and data[keys["nopid"]]["last_port"] == good
    assert data[str(ws.root.resolve())]["pid"] == me  # our own entry is never probed or pruned


def test_prune_spares_an_entry_that_registered_again(ws, tmp_path):
    key = _register_other(tmp_path, "back", 9001, pid=111)
    _register_other(tmp_path, "back", 9002, pid=222)  # restarted on another port meanwhile
    switcher._prune([(key, 111, 9001)])
    assert _entries()[key]["pid"] == 222


# -- never on the page render's clock ---------------------------------------------------------------------------------

def test_others_never_waits_for_a_scan(ws, monkeypatch):
    release, calls = threading.Event(), []

    def slow_scan(w):
        calls.append(w)
        release.wait(5)
        return [{"name": "northwind", "url": "http://127.0.0.1:8766/", "needs": None}]

    monkeypatch.setattr(switcher, "scan", slow_scan)
    t0 = time.monotonic()
    assert switcher.others(ws) == [] and switcher.others(ws) == []
    assert time.monotonic() - t0 < 1.0
    release.set()
    deadline = time.monotonic() + 5
    while not switcher.others(ws) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert switcher.others(ws)[0]["name"] == "northwind"
    assert len(calls) == 1  # one scan in flight at a time, then cached for REFRESH_SECONDS


def test_others_survives_a_failing_scan(ws, monkeypatch):
    def boom(w):
        raise RuntimeError("x")

    monkeypatch.setattr(switcher, "scan", boom)
    assert switcher.others(ws) == []
    time.sleep(0.05)
    assert switcher.others(ws) == []


# -- clean shutdown ------------------------------------------------------------------------------------------------

def test_clean_shutdown_clears_our_pid(ws):
    key = str(ws.root.resolve())
    with _local(_app(ws, port=8765)):
        assert _entries()[key]["pid"] == os.getpid()
    entry = _entries()[key]
    assert "pid" not in entry and entry["last_port"] == 8765  # the remembered port stays


def test_unregister_leaves_another_process_entry(ws):
    switcher.register(ws, 8765)
    from orch.addons.userfiles import update_json
    from orch.dashboard.launch import config_dir
    key = str(ws.root.resolve())
    update_json(config_dir() / "workspaces.json", lambda d: d[key].update(pid=424242))
    switcher.unregister(ws)
    assert _entries()[key]["pid"] == 424242


# -- opt-in ------------------------------------------------------------------------------------------------------

def test_switcher_is_off_by_default_and_never_probes(dash, monkeypatch):
    monkeypatch.setattr(switcher, "others", lambda ws: pytest.fail("the switcher is off"))
    html = dash.get("/").text
    assert "menu-switcher" not in html
    assert "Turn switcher on" in dash.get("/workspace").text


def test_turning_the_switcher_on_shows_other_workspaces(dash, monkeypatch):
    monkeypatch.setattr(switcher, "others", lambda ws: [{"name": "northwind", "url": "http://127.0.0.1:8766/", "needs": 2}])
    r = dash.post("/workspace/switcher", data={"on": "1"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    html = dash.get("/").text
    assert "menu-switcher" in html and 'href="http://127.0.0.1:8766/"' in html and "northwind: 2 waiting" in html
    assert "Turn switcher off" in dash.get("/workspace").text
    dash.post("/workspace/switcher", data={"on": "0"}, headers={"origin": "http://testserver"})
    assert "menu-switcher" not in dash.get("/").text


def test_switcher_toggle_needs_same_origin(dash):
    r = dash.post("/workspace/switcher", data={"on": "1"}, follow_redirects=False)
    assert r.status_code == 403
    assert "Turn switcher on" in dash.get("/workspace").text
