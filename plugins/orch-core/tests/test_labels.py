"""#173: labels set from the CLI (`orch new --label`, `orch label add/remove`), checked, logged and evented."""
import json

import pytest

from orch.cli import run
from orch.core import query, store
from orch.core.events import read_events
from orch.core.ops import check_labels
from orch.errors import UsageError
from orch.hooks.guard import evaluate


@pytest.fixture
def agent_cli(monkeypatch, ws_root):
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    return ws_root


def _json(capsys, *args):
    code = run([*args, "--json"])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


# -- names ---------------------------------------------------------------------------------

def test_label_names_are_one_word_each_and_kept_once():
    assert check_labels(["customer:arbonia", "admin", "admin", "knowledge"]) == ["customer:arbonia", "admin", "knowledge"]


@pytest.mark.parametrize("bad", ["", "two words", "a,b", "tab\there", "new\nline", "zero​width", "x" * 65])
def test_bad_label_names_are_refused(bad):
    with pytest.raises(UsageError):
        check_labels([bad])


# -- orch new --label ----------------------------------------------------------------------

def test_new_sets_labels(agent_cli, ws, capsys):
    code, view, _ = _json(capsys, "new", "--title", "Onboard Arbonia", "--label", "customer:arbonia",
                          "--label", "admin", "--label", "admin")
    assert code == 0
    t = store.load(ws, view["id"])[1]
    assert t.meta["labels"] == ["customer:arbonia", "admin"]
    created = [e for e in read_events(ws, t.id) if e.kind == "ticket.created"]
    assert created[0].data["labels"] == ["customer:arbonia", "admin"]
    assert [e.id for e in query.list_tickets(ws, label="customer:arbonia")] == [t.id]


def test_new_refuses_a_bad_label_before_creating_anything(agent_cli, ws, capsys):
    code = run(["new", "--title", "x", "--label", "two words"])
    assert code != 0 and "label 'two words'" in capsys.readouterr().err
    assert store.scan(ws) == []


def test_new_without_labels_keeps_an_empty_list(aops, ws):
    t = aops.new("x")
    assert store.load(ws, t.id)[1].meta["labels"] == []


# -- orch label add / remove ----------------------------------------------------------------

def test_an_agent_adds_and_removes_labels_with_a_log_line_and_an_event(agent_cli, ws, aops, capsys):
    t = aops.new("x")
    code, view, _ = _json(capsys, "label", "add", t.id, "customer:arbonia", "admin")
    assert code == 0 and view["id"] == t.id
    assert store.load(ws, t.id)[1].meta["labels"] == ["customer:arbonia", "admin"]
    code, _, _ = _json(capsys, "label", "remove", t.id, "admin")
    assert code == 0
    ticket = store.load(ws, t.id)[1]
    assert ticket.meta["labels"] == ["customer:arbonia"]
    log = ticket.section("Log")
    assert "labels added customer:arbonia, admin" in log and "labels removed admin" in log
    edits = [e.data for e in read_events(ws, t.id) if e.kind == "ticket.edited"]
    assert edits == [{"labels_added": ["customer:arbonia", "admin"], "labels_removed": []},
                     {"labels_added": [], "labels_removed": ["admin"]}]


def test_a_label_already_there_or_not_there_changes_nothing(ws, aops):
    t = aops.new("x", labels=["admin"])
    before = len(read_events(ws, t.id))
    aops.label(t.id, add=["admin"])
    aops.label(t.id, remove=["knowledge"])
    ticket = store.load(ws, t.id)[1]
    assert ticket.meta["labels"] == ["admin"] and "labels" not in ticket.section("Log")
    assert len(read_events(ws, t.id)) == before


def test_add_skips_the_ones_it_has_and_adds_the_rest(ws, aops):
    t = aops.new("x", labels=["admin"])
    aops.label(t.id, add=["admin", "knowledge"])
    ticket = store.load(ws, t.id)[1]
    assert ticket.meta["labels"] == ["admin", "knowledge"]
    assert "labels added knowledge" in ticket.section("Log")


def test_label_needs_a_name_and_a_good_one(ws, aops):
    t = aops.new("x")
    with pytest.raises(UsageError):
        aops.label(t.id)
    with pytest.raises(UsageError):
        aops.label(t.id, add=["a,b"])
    assert store.load(ws, t.id)[1].meta["labels"] == []


def test_label_text_output(agent_cli, ws, aops, capsys):
    t = aops.new("x")
    assert run(["label", "add", t.id, "admin"]) == 0
    assert f"{t.id}: labels admin" in capsys.readouterr().out
    assert run(["label", "remove", t.id, "admin"]) == 0
    assert f"{t.id}: labels none" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", ["orch label add L-0001 customer:arbonia", "orch label remove L-0001 admin",
                                 "orch new --title x --label admin"])
def test_the_guard_lets_agents_set_labels(ws, cmd):
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow


# -- dashboard ------------------------------------------------------------------------------

def test_the_dashboard_new_form_takes_labels(dash, ws):
    assert 'name="labels"' in dash.get("/new").text
    r = dash.post("/new", data={"title": "Onboard Arbonia", "labels": "customer:arbonia, admin admin"})
    assert r.status_code == 200 and "created L-0001 in backlog" in r.text
    assert store.load(ws, "L-0001")[1].meta["labels"] == ["customer:arbonia", "admin"]


def test_the_dashboard_new_form_refuses_a_bad_label(dash, ws):
    r = dash.post("/new", data={"title": "x", "labels": "ok bad​one"})
    assert r.status_code == 422 and store.scan(ws) == []


def test_the_timeline_names_a_label_change(ws, aops):
    from orch.dashboard.data.timeline import action_phrase, describe
    t = aops.new("x")
    aops.label(t.id, add=["admin"])
    ev = [e for e in read_events(ws, t.id) if e.kind == "ticket.edited"][-1]
    assert action_phrase(ev) == "changed the labels" and describe(ev) == "changed the labels"
