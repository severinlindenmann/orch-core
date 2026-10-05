from pathlib import Path

import pytest
from test_default_addons import recordings

from orch.addons import userfiles
from orch.addons.scheduler import Scheduler
from orch.core import store
from orch.core.events import read_events
from orch.testing import FakeRunner, fake_workspace

ADDON = Path(__file__).resolve().parents[1] / "addons" / "github-issues"
ORIGIN = {"origin": "http://testserver"}
GH = {"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://github.com/acme/ticket-orch-demo/issues/{id}"}
TICKETS = [{"title": "Retry gateway downloads", "status": "testing", "external": ["GH-1"]},
           {"title": "Consistent timestamp formats", "status": "in-progress", "external": ["GH-13"]},
           {"title": "Quality report divides by zero", "status": "done", "external": ["GH-5"]},
           {"title": "Partition mart by month", "status": "open", "external": ["GH-6"]}]


@pytest.fixture
def mc(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    fw = fake_workspace(tmp_path / "demo", prefix="DEMO", trackers=[GH], tickets=TICKETS)
    userfiles.set_enabled(fw.root, "github-issues", True)
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    client = TestClient(create_app(fw.ws, "tok"))
    assert client.get("/?token=tok").status_code == 200
    la = fw.ws.addons.get("github-issues")
    la.ctx.runner = FakeRunner(recordings(ADDON))
    Scheduler(fw.ws, live=lambda: True).step()
    return fw, client


def _post(client, action, target):
    return client.post(f"/addons/github-issues/actions/{action}", data={"target": target}, headers=ORIGIN, follow_redirects=False)


def test_external_tab_shows_mine_sprint_and_out_of_sync(mc):
    _, client = mc
    html = client.get("/board?view=external").text
    assert 'href="/board?view=external"' in html and "GitHub issues · Mine" in html and "GH-11" in html
    assert "Sprint 42" in html and "Sprint · Sprint 42" not in html and "Out of sync" in html
    assert 'action="/addons/github-issues/actions/close_local"' in html and 'data-dialog="Close DEMO-0002 ' in html
    assert "GitHub status" in html and "Changes only the local ticket in orch" in html and "GitHub milestones" in html
    # GI-04: freshness and a Refresh are on the External tab itself
    assert "Updated" in html and 'action="/addons/github-issues/refresh"' in html


def test_ticket_page_shows_the_issue(mc):
    _, client = mc
    html = client.get("/t/DEMO-0003").text
    assert "GH-5 · GitHub issue" in html and "Open again in GitHub, done here." in html
    assert 'action="/addons/github-issues/actions/reopen_local"' in html


def test_close_local_is_a_human_post(mc):
    fw, client = mc
    r = _post(client, "close_local", "DEMO-0002")
    assert r.status_code == 303 and "Closed+DEMO-0002" in r.headers["location"]
    assert store.load(fw.ws, "DEMO-0002")[1].status == "done"
    moved = [e for e in read_events(fw.ws) if e.kind == "ticket.moved"][-1]
    assert moved.actor == "human:you" and moved.via == "dashboard" and moved.data["reason"] == "GH-13 is closed in GitHub"
    action = [e for e in read_events(fw.ws) if e.kind == "addon.action"][-1]
    assert action.data["action"] == "close_local" and action.data["intent"] == "close" and action.actor == "human:you"


def test_stale_close_is_refused(mc):
    fw, client = mc
    r = _post(client, "close_local", "DEMO-0004")
    assert "err=" in r.headers["location"] and store.load(fw.ws, "DEMO-0004")[1].status == "open"


def test_forged_posts_change_nothing(mc):
    fw, client = mc
    assert "err=" in _post(client, "import", "GH-404").headers["location"]
    assert "err=" in _post(client, "close_local", "DEMO-0002|GH-13").headers["location"]
    assert "err=" in _post(client, "ignore", "DEMO-0001|GH-1|todo").headers["location"]
    # the title comes from the cache, never from the form
    r = client.post("/addons/github-issues/actions/import", data={"target": "GH-9", "title": "<b>forged</b>"},
                    headers=ORIGIN, follow_redirects=False)
    assert "Imported+GH-9+as+DEMO-0005" in r.headers["location"]
    assert store.load(fw.ws, "DEMO-0005")[1].title == "Handle late-arriving gateway exports"
    assert store.load(fw.ws, "DEMO-0002")[1].status == "in-progress" and len(store.scan(fw.ws)) == 5


def test_import_and_ignore(mc):
    fw, client = mc
    assert "Imported+GH-9+as+DEMO-0005" in _post(client, "import", "GH-9").headers["location"]
    assert "already+DEMO-0005" in _post(client, "import", "GH-9").headers["location"]
    assert store.load(fw.ws, "DEMO-0005")[1].status == "backlog"
    _post(client, "ignore", "DEMO-0003|GH-5|todo")
    assert "Reopen local" not in client.get("/t/DEMO-0003").text


def test_saved_default_filter(mc):
    _, client = mc
    r = client.post("/workspace/addons/github-issues/settings", data={"default_filter": "all"}, headers=ORIGIN, follow_redirects=False)
    assert "Saved+settings" in r.headers["location"]
    assert "GitHub issues · All open" in client.get("/board?view=external").text


def test_actions_need_origin(mc):
    fw, client = mc
    assert client.post("/addons/github-issues/actions/close_local", data={"target": "DEMO-0002"},
                       follow_redirects=False).status_code == 403
    assert store.load(fw.ws, "DEMO-0002")[1].status == "in-progress"


def test_disabled_shows_no_external_tab(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    fw = fake_workspace(tmp_path / "off", trackers=[GH])
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(fw.ws, "tok"))
    c.get("/?token=tok")
    assert "view=external" not in c.get("/board").text
