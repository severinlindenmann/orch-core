import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orch.addons import cache, discovery, userfiles
from orch.addons.api import Snapshot
from orch.addons.check import static_problems
from orch.testing import PAYLOAD, FakeRunner, run_addon_contract

PLUGIN = Path(__file__).resolve().parents[1]
ADDONS = {"databricks": PLUGIN / "addons" / "databricks", "wiki": PLUGIN / "addons" / "wiki"}
ORIGIN = {"origin": "http://testserver"}
SPACE = "acme/ticket-orch-demo"
HINT = "documents files this ticket changed — may need an update"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """The databricks addon reads ~/.databrickscfg; never the real one in tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.parametrize("name", sorted(ADDONS))
def test_default_addon_passes_orch_addon_check(name):
    assert static_problems(ADDONS[name]) == []
    assert run_addon_contract(ADDONS[name], runner=FakeRunner(strict=False)) == []


@pytest.mark.parametrize("name", sorted(ADDONS))
def test_shipped_as_a_trusted_default_that_is_off(ws, name):
    found = discovery.find(name)
    assert found is not None and found.kind == "default" and found.error is None
    assert userfiles.trust_state(found) == "trusted"
    assert userfiles.workspace_addons(ws.root).get(name, {}).get("enabled") is not True
    assert ws.addons.get(name) is None  # off by default, so never imported


@pytest.fixture
def client(ws, put, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app

    tid = put("testing", title="Move loader", branches={"harness": "feature/x"})
    for name in ADDONS:
        userfiles.set_enabled(ws.root, name, True)
    userfiles.save_addon_config(ws.root, "databricks", {"envs": {"int": "simulated:int", "prod": "simulated:prod"},
                                                        "demo": True, "scope": "all"})
    userfiles.save_addon_config(ws.root, "wiki", {"provider": "github-wiki", "repos": SPACE})
    ws._addons = None
    registry = ws.addons
    assert registry.get("databricks") and registry.get("wiki"), registry.problems
    dbx = registry.get("databricks")
    for scope in ("int", "prod"):
        snap = dbx.providers()[0].fetch(dbx.ctx.provider_context(runner=FakeRunner()), scope, None)  # simulated: no CLI
        snap = snap.replace(items=tuple({**i, "name": PAYLOAD} if i.get("run_id") == "9001" else i for i in snap.items))
        cache.write_snapshot(ws, "databricks", snap)
    now = datetime.now(timezone.utc)
    page = {"provider": "github-wiki", "space": SPACE, "id": "Architecture", "title": f"Architecture {PAYLOAD}",
            "url": f"https://github.com/{SPACE}/wiki/Architecture", "path": "Architecture.md", "updated_at": now.isoformat(),
            "excerpt": "", "links": [], "documents": ["src/**"], "file_links": []}
    cache.write_snapshot(ws, "wiki", Snapshot("github-wiki", SPACE, now, items=(page,)))
    diff = {"id": f"{tid}:harness", "label": tid, "role": "info", "text": "1 file(s) changed", "ticket": tid,
            "status": "testing", "repo": "harness", "branch": "feature/x", "files": ["src/a.py"]}
    cache.write_snapshot(ws, "wiki", Snapshot("branch-diffs", "tickets", now, items=(diff,)))
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    c.tid = tid
    return c


def test_pages_render_from_the_cache_without_commands(client, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("subprocess or socket during a page render")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for url in ("/", f"/t/{client.tid}", "/addons/databricks/", "/addons/wiki/", "/addons/wiki/?q=architecture"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert PAYLOAD not in r.text, url


def test_databricks_page_today_and_create_ticket(client, ws):
    from orch.core import store
    from orch.core.events import read_events
    html = client.get("/addons/databricks/").text
    assert "int, prod are simulated (demo)" in html and "&lt;script&gt;" in html
    assert 'action="/addons/databricks/actions/create_ticket"' in html
    assert 'data-dialog="Create a backlog ticket for this failed run?"' in html
    today = client.get("/").text
    assert "Databricks failures" in today and "Databricks: 1 failed run" in today
    target = "int|501|9001|acme-ingest-daily"
    assert client.post("/addons/databricks/actions/create_ticket", data={"target": target},
                       follow_redirects=False).status_code == 403  # no Origin
    from fastapi.testclient import TestClient
    no_cookie = TestClient(client.app).post("/addons/databricks/actions/create_ticket", data={"target": target},
                                            headers=ORIGIN, follow_redirects=False)
    assert no_cookie.status_code == 401  # no token cookie: refused by the dashboard's auth
    assert not any(x.get("key") == "DBX-9001" for e in store.scan(ws) for x in (e.meta or {}).get("external") or [])
    r = client.post("/addons/databricks/actions/create_ticket", data={"target": target}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    created = [e for e in store.scan(ws) if any(x.get("key") == "DBX-9001" for x in (e.meta or {}).get("external") or [])]
    assert len(created) == 1 and created[0].status == "backlog"
    assert any(ev.kind == "addon.action" and ev.data == {"addon": "databricks", "action": "create_ticket", "target": target}
               for ev in read_events(ws))


def test_wiki_hint_on_ticket_and_today_then_dismiss(client):
    tid = client.tid
    assert HINT in client.get(f"/t/{tid}").text
    today = client.get("/").text
    assert "From addons" in today and f"documents files {tid} changed — may need an update" in today
    decision = f"docs|{tid}|{SPACE}|Architecture"
    assert client.post("/addons/wiki/decisions", data={"id": decision, "choice": "dismiss"},
                       follow_redirects=False).status_code == 403
    r = client.post("/addons/wiki/decisions", data={"id": decision, "choice": "dismiss"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert HINT not in client.get(f"/t/{tid}").text


def test_wiki_search_box_is_escaped(client):
    html = client.get("/addons/wiki/", params={"q": "<b>arch</b>"}).text
    assert 'role="search"' in html and "&lt;b&gt;arch&lt;/b&gt;" in html and "<b>arch</b>" not in html


def test_workspace_shows_both_with_their_settings(client):
    html = client.get("/workspace").text
    for text in ("Databricks", "Wiki", "Demo workspace (allows simulated environments)", "Wiki provider"):
        assert text in html
