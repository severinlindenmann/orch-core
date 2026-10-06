"""Quick tasks in Mission Control: the page, the switch, the human's actions and the remote gate."""
import pytest

from orch.core import quick
from orch.core.quick import QuickOps


@pytest.fixture
def client(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _post(client, url, **form):
    return client.post(url, data=form, follow_redirects=False, headers={"Origin": "http://testserver"})


def test_page_off_offers_the_switch(client):
    r = client.get("/quick")
    assert r.status_code == 200
    assert "Quick tasks are off" in r.text and "Turn on quick tasks" in r.text
    assert 'href="/quick"' in r.text  # the menu item


def test_turn_on_add_done_reopen(client, ws):
    r = _post(client, "/workspace/quick", enabled="1", max_commits="1", max_files="3")
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    assert quick.enabled(ws)
    r = _post(client, "/quick/add", title="Fix the README typo", area="README.md", next="/quick")
    assert "added+Q-1" in r.headers["location"]
    page = client.get("/quick").text
    assert "Fix the README typo" in page and "README.md" in page and "1 open" in page
    _post(client, "/quick/Q-1/done", next="/quick")
    assert quick.load(ws, "Q-1")["status"] == "done"
    detail = client.get("/quick/Q-1").text
    assert "Done with" in detail and "Reopen" in detail
    _post(client, "/quick/Q-1/reopen", note="still a typo in line 3", next="/quick/Q-1")
    t = quick.load(ws, "Q-1")
    assert t["status"] == "open" and t["notes"][0]["text"] == "still a typo in line 3"


def test_promote_and_drop(client, ws, human):
    quick.set_settings(ws, human, enabled=True)
    QuickOps(ws, human).add("Rename the flag")
    QuickOps(ws, human).add("Stale fixture")
    r = _post(client, "/quick/Q-1/promote", next="/quick")
    assert "L-0001" in r.headers["location"]
    assert quick.load(ws, "Q-1")["ticket"] == "L-0001"
    _post(client, "/quick/Q-2/drop", next="/quick")
    assert quick.load(ws, "Q-2")["status"] == "dropped"


def test_outgrown_task_shows_the_human_its_choices(client, ws, human, agent, monkeypatch):
    quick.set_settings(ws, human, enabled=True)
    qid = QuickOps(ws, human).add("Rename the flag")["id"]
    QuickOps(ws, agent).claim(qid)
    monkeypatch.setattr(quick, "size", lambda ws, q: {"commits": ["a", "b"], "files": [f"f{i}" for i in range(6)]})
    with pytest.raises(Exception):
        QuickOps(ws, agent).done(qid, "renamed")
    page = client.get(f"/quick/{qid}").text
    assert "Bigger than a quick task" in page and "Make it a ticket" in page and "Let it finish" in page
    assert "outgrew it" in client.get("/quick").text


def test_artifacts_on_the_detail_page(client, ws, human, agent, tmp_path):
    quick.set_settings(ws, human, enabled=True)
    qid = QuickOps(ws, human).add("Contrast")["id"]
    QuickOps(ws, agent).claim(qid)
    shot = tmp_path / "after.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    QuickOps(ws, agent).artifact_add(qid, shot, label="After")
    QuickOps(ws, agent).artifact_add(qid, url="https://example.com/r/1", label="CI run")
    page = client.get(f"/quick/{qid}").text
    assert f'src="/a/{qid}/after.png"' in page and "CI run" in page
    assert client.get(f"/a/{qid}/after.png").status_code == 200


def test_errors_come_back_as_messages(client, ws):
    r = _post(client, "/quick/add", title="x", next="/quick")
    assert "err=" in r.headers["location"]  # off
    r = client.get("/quick/Q-99", follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_every_quick_route_has_a_remote_tag():
    from orch.dashboard import remote_gate
    from orch.dashboard.app import router_modules
    from orch.dashboard import routes_quick
    assert routes_quick in router_modules()
    for route in routes_quick.router.routes:
        for method in route.methods:
            assert (method, route.path) in remote_gate.TAGS, (method, route.path)
    assert remote_gate.TAGS[("POST", "/workspace/quick")] is remote_gate.NO
