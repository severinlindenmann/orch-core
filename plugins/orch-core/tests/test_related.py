import json

from orch.cli import run


def test_ticket_finds_history_in_flight_and_co_changed(ws, ticket_history):
    from orch.core.related import related
    data = related(ws, ticket_history["mine"])
    assert data["paths"] == ["src/auth/login.py"]
    assert [t["id"] for t in data["history"]] == [ticket_history["login"], ticket_history["sess"]]
    first = data["history"][0]
    assert first["commits"] == 2 and first["summary"] == "Login with email" and first["findings"] == "Keep sessions server side"
    assert [t["id"] for t in data["in_flight"]] == [ticket_history["other"]]  # from an unmerged branch
    assert data["co_changed"][0] == {"file": "src/auth/session.py", "commits": 3, "of": 5}
    assert all(c["file"] != "README.md" for c in data["co_changed"])


def test_paths_without_ticket_and_folder_match(ws, ticket_history, ticket_repo):
    from orch.core.related import related
    data = related(ws, None, [str(ticket_repo / "src" / "auth")])
    assert data["paths"] == ["src/auth"] and data["ticket"] is None
    ids = {t["id"] for t in data["history"]} | {t["id"] for t in data["in_flight"]}
    assert ids == set(ticket_history.values())
    assert related(ws, None, ["docs/nothing.md"])["commits"] == 0


def test_linked_both_directions(ws, put):
    from orch.core.related import related
    epic = put("open", title="Auth epic", type="epic")
    a = put("open", title="A", parent=epic)
    b = put("backlog", title="B", blocked_by=[a])
    rel = {(t["relation"], t["id"]) for t in related(ws, a)["linked"]}
    assert rel == {("parent", epic), ("blocks", b)}
    assert {(t["relation"], t["id"]) for t in related(ws, epic)["linked"]} == {("child", a)}


def test_cli_text_and_json(ws, ticket_history, capsys):
    assert run(["related", ticket_history["mine"]]) == 0
    out = capsys.readouterr().out
    assert "history:" in out and "in flight:" in out and "co-changed:" in out and "src/auth/session.py" in out
    assert run(["related", "--path", "src/auth/session.py", "--json", "--limit", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["history"]) == 1
    assert run(["related"]) == 2


def test_no_git_checkout_still_shows_links(ws, put, capsys):
    t = put("open", title="Lonely")
    assert run(["related", t]) == 0
    assert "no git checkout" in capsys.readouterr().out
