"""Quick tasks in Mission Control: the page, the switch, the human's actions and the remote gate."""
import pytest

from orch.core import quick
from orch.core.quick import QuickOps


def turn_on(ws, **settings):
    from orch.addons import userfiles
    userfiles.set_enabled(ws.root, quick.ADDON, True)
    if settings:
        userfiles.save_addon_config(ws.root, quick.ADDON, settings)


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


def test_off_hidden_from_the_menu_and_404(client):
    r = client.get("/quick")
    assert r.status_code == 404 and "Workspace &amp; addons" in r.text
    assert 'href="/quick"' not in client.get("/").text
    assert client.get("/quick/Q-1").status_code == 404


def test_the_addon_is_listed_in_workspace_and_addons(client):
    page = client.get("/workspace").text
    assert 'id="addon-quick-tasks"' in page and "Agents may add quick tasks" in page


def test_on_shows_the_menu_item_under_addons(client, ws, human):
    turn_on(ws)
    QuickOps(ws, human).add("Typo")
    home = client.get("/").text
    addons = home[home.index('id="menu-addons-h"'):]
    assert 'href="/quick"' in addons and "1 open" in addons


def test_add_done_reopen(client, ws):
    turn_on(ws)
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


@pytest.fixture
def started(monkeypatch):
    """launch.start recorded instead of run; terminals on or off per test."""
    from orch.dashboard import launch, terminals
    calls = []
    monkeypatch.setattr(launch, "preflight", lambda terminal, settings: None)
    monkeypatch.setattr(launch, "start", lambda ws, key, argv, **kw: calls.append((key, argv, kw)) or f"Opened in {kw['terminal']}")
    monkeypatch.setattr(terminals, "free_name", lambda ws, base: base)
    return calls


def test_open_in_terminal_starts_an_agent_with_the_quick_prompt(client, ws, human, started):
    turn_on(ws)
    QuickOps(ws, human).add("Typo in <README>")
    page = client.get("/quick").text
    assert 'name="where" value=""' in page and 'name="where" value="tmux"' not in page  # Terminals addon off
    r = _post(client, "/quick/Q-1/agent/start", where="", next="/quick")
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    ((key, argv, kw),) = started
    assert key == "Q-1" and kw["terminal"] != "tmux"
    prompt = " ".join(argv)
    assert "orch quick claim Q-1" in prompt and "README" not in prompt  # the key only, never the task's text


def test_open_in_mission_control_needs_terminals(client, ws, human, started, monkeypatch):
    from orch.dashboard import terminals
    turn_on(ws)
    QuickOps(ws, human).add("Typo")
    r = _post(client, "/quick/Q-1/agent/start", where="tmux", next="/quick")
    assert "err=" in r.headers["location"] and not started
    monkeypatch.setattr(terminals, "enabled", lambda ws, request=None: True)
    assert 'name="where" value="tmux"' in client.get("/quick").text
    r = _post(client, "/quick/Q-1/agent/start", where="tmux", next="/quick")
    assert r.headers["location"].startswith("/terminals/Q-1")
    assert started[0][2]["terminal"] == "tmux"


def test_no_start_on_a_claimed_or_outgrown_task(client, ws, human, agent, started, monkeypatch):
    turn_on(ws)
    QuickOps(ws, human).add("Typo")
    QuickOps(ws, agent).claim("Q-1")
    r = _post(client, "/quick/Q-1/agent/start", where="", next="/quick")
    assert "already+claimed" in r.headers["location"] and not started
    monkeypatch.setattr(quick, "size", lambda ws, q: {"commits": ["a", "b"], "files": []})
    with pytest.raises(Exception):
        QuickOps(ws, agent).done("Q-1", "x")
    r = _post(client, "/quick/Q-1/agent/start", where="", next="/quick")
    assert "outgrown" in r.headers["location"] and not started
    assert "Make it a ticket" in client.get("/quick").text


def test_promote_and_drop(client, ws, human):
    turn_on(ws)
    QuickOps(ws, human).add("Rename the flag")
    QuickOps(ws, human).add("Stale fixture")
    r = _post(client, "/quick/Q-1/promote", next="/quick")
    assert "L-0001" in r.headers["location"]
    assert quick.load(ws, "Q-1")["ticket"] == "L-0001"
    _post(client, "/quick/Q-2/drop", next="/quick")
    assert quick.load(ws, "Q-2")["status"] == "dropped"


def test_outgrown_task_shows_the_human_its_choices(client, ws, human, agent, monkeypatch):
    turn_on(ws)
    qid = QuickOps(ws, human).add("Rename the flag")["id"]
    QuickOps(ws, agent).claim(qid)
    monkeypatch.setattr(quick, "size", lambda ws, q: {"commits": ["a", "b"], "files": [f"f{i}" for i in range(6)]})
    with pytest.raises(Exception):
        QuickOps(ws, agent).done(qid, "renamed")
    page = client.get(f"/quick/{qid}").text
    assert "Bigger than a quick task" in page and "Make it a ticket" in page and "Let it finish" in page
    assert "outgrew it" in client.get("/quick").text


def test_artifacts_on_the_detail_page(client, ws, human, agent, tmp_path):
    turn_on(ws)
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
    turn_on(ws)
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
    assert remote_gate.TAGS[("POST", "/quick/{qid}/agent/start")] == remote_gate.Tag(remote_gate.Scope.TYPE, fresh=True)
