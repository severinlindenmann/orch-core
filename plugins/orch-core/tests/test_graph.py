import html
import json
import re

import pytest

from orch.cli import run


def _client(ws, graph: bool):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.addons import userfiles
    from orch.dashboard.app import create_app
    if graph:  # the `graph` default addon is the page's switch (#167)
        userfiles.set_enabled(ws.root, "graph", True)
    client = TestClient(create_app(ws, "tok"))
    assert client.get("/?token=tok").status_code == 200
    return client


@pytest.fixture
def dash(ws):
    return _client(ws, graph=True)


def test_off_by_default_no_menu_and_404(ws, ticket_history):
    off = _client(ws, graph=False)
    r = off.get("/graph")
    assert r.status_code == 404 and "Graph is an addon" in r.text and 'id="graph-data"' not in r.text
    assert off.get("/graph.json").status_code == 404
    assert off.get(f"/graph/related?t={ticket_history['mine']}").status_code == 404
    assert 'href="/graph"' not in off.get("/board").text
    assert run(["graph", "--json"]) == 0  # the CLI stays core


def test_build_nodes_edges_and_collisions(ws, ticket_history, put):
    from orch.core.graph import build
    data = build(ws)
    kinds = {n["id"]: n["kind"] for n in data["nodes"]}
    assert kinds[ticket_history["mine"]] == "ticket" and kinds["src/auth/login.py"] == "file"
    changed = {(e["source"], e["target"]): e.get("weight") for e in data["edges"] if e["kind"] == "changed"}
    assert changed[(ticket_history["login"], "src/auth/login.py")] == 2
    assert data["collisions"] == [{"file": "src/auth/login.py", "tickets": sorted([ticket_history["mine"], ticket_history["other"]])}]
    assert data["commits"] == 5 and data["repos"] == ["."]


def test_links_and_epic(ws, put):
    from orch.core.graph import build
    epic = put("open", title="Auth", type="epic")
    a = put("open", title="A", parent=epic)
    b = put("backlog", title="B", blocked_by=[a])
    f = put("open", title="F", parent=a)
    data = build(ws)
    edges = {(e["source"], e["target"], e["kind"]) for e in data["edges"]}
    assert {(epic, a, "parent"), (a, b, "blocks"), (a, f, "follow_up")} <= edges
    epics = {n["id"]: n["epic"] for n in data["nodes"]}
    assert epics[a] == epic and epics[f] == epic and epics[b] is None and epics[epic] == epic
    assert data["repos"] == [] and data["collisions"] == []


def test_max_files_keeps_the_most_changed(ws, ticket_history):
    from orch.core.graph import build
    data = build(ws, max_files=1)
    assert [n["id"] for n in data["nodes"] if n["kind"] == "file"] == ["src/auth/login.py"]
    assert data["files_total"] > 1
    assert all(e["target"] == "src/auth/login.py" for e in data["edges"] if e["kind"] == "changed")


def test_cli(ws, ticket_history, capsys):
    assert run(["graph"]) == 0
    out = capsys.readouterr().out
    assert "collisions" in out and "src/auth/login.py" in out
    assert run(["graph", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["collisions"]


def test_page_embeds_the_data_and_is_in_the_menu(dash, ticket_history, ws):
    r = dash.get("/graph")
    assert r.status_code == 200
    page = r.text
    assert len(re.findall(r"<h1\b", page)) == 1
    assert re.search(r'href="/graph"\s+aria-current="page"', page)
    blob = re.search(r'<script type="application/json" id="graph-data">(.*?)</script>', page, re.S).group(1)
    data = json.loads(blob)
    assert data["collisions"][0]["file"] == "src/auth/login.py"
    assert "/static/graph.js" in page and "/static/graph.css" in page
    assert "<code>src/auth/login.py</code>" in page  # the collision list works without JS
    board = dash.get("/board").text
    assert 'href="/graph"' in board.split('class="menu-addons"')[1]  # every page's menu links it, under Addons


def test_page_escapes_ticket_text_in_the_json_block(dash, put):
    put("open", title="</script><script>alert(1)</script>")
    page = dash.get("/graph").text
    blob = re.search(r'<script type="application/json" id="graph-data">(.*?)</script>', page, re.S).group(1)
    assert "<script>" not in blob and json.loads(blob)["nodes"][0]["title"].startswith("</script>")


def test_views_and_focus(dash, ticket_history):
    page = dash.get(f"/graph?view=local&t={ticket_history['mine'].lower()}").text
    assert f'data-view="local" data-focus="{ticket_history["mine"]}"' in page
    assert 'data-view="code" data-focus=""' in dash.get("/graph?view=bogus&t=nope").text


def test_json_and_related_endpoints(dash, ticket_history):
    data = dash.get("/graph.json").json()
    assert any(n["kind"] == "file" for n in data["nodes"])
    r = dash.get(f"/graph/related?t={ticket_history['mine']}").json()
    assert "history:" in r["text"] and r["data"]["ticket"]["id"] == ticket_history["mine"]
    r = dash.get("/graph/related", params={"p": "src/auth/session.py"}).json()
    assert r["data"]["paths"] == ["src/auth/session.py"]
    assert dash.get("/graph/related").status_code == 400
    assert dash.get("/graph/related?t=L-9999").status_code == 404


def test_needs_you_is_marked(dash, put, aops):
    t = aops.new("Needs requirements")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    nodes = {n["id"]: n for n in dash.get("/graph.json").json()["nodes"]}
    assert nodes[t.id].get("needs")
    assert html.escape(nodes[t.id]["needs"]) in dash.get("/graph").text
