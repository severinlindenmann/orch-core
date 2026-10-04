"""Whose move it is, as the dashboard's rules decide it (data.cards), in `orch show --json`, `orch list --json` and the
ticket document: one copy of the rules, including the cases a client cannot derive without the gate hashes."""
import json

import pytest

from orch.cli import run
from orch.core import store
from orch.core.schema import SCHEMA_VERSION, ticket_document, ticket_schema


def _refine(ops, tid, *, plan="1. do it", req="r", ac="- [ ] a"):
    ops.set_section(tid, "Requirements", req)
    ops.set_section(tid, "Acceptance criteria", ac)
    if plan:
        ops.set_section(tid, "Plan", plan)
    return tid


def _show(capsys, tid):
    capsys.readouterr()
    assert run(["show", tid, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["move"]


def _list(capsys):
    capsys.readouterr()
    assert run(["list", "--json"]) == 0
    return {r["id"]: r["move"] for r in json.loads(capsys.readouterr().out)}


def test_show_and_list_carry_the_move(ws, aops, capsys):
    t = aops.new("x")
    _refine(aops, t.id, plan=None)
    move = _show(capsys, t.id)
    assert move["who"] == "you" and move["kind"] == "approve-requirements" and move["label"] == "Approve requirements"
    assert move["why"]
    assert _list(capsys)[t.id] == move


def test_the_agents_move_has_no_why(ws, put, capsys):
    tid = put("open", title="ready")
    move = _show(capsys, tid)
    assert move == {"who": "agent", "kind": "ready", "label": "Ready", "ref": None}


def test_plan_approval_and_verdict(ws, aops, working, put, capsys):
    aops.set_section(working, "Plan", "1. do it")
    assert _show(capsys, working)["kind"] == "approve-plan"
    tested = put("testing", title="t")
    assert _show(capsys, tested)["kind"] == "verdict"


def test_changes_requested_then_the_text_changed_is_the_humans_move(ws, aops, hops, capsys):
    from orch.core.gates import gate_hash
    t = aops.new("x")
    _refine(aops, t.id, plan=None)
    seen = gate_hash(store.load(ws, t.id)[1], "requirements")
    hops.request_changes(t.id, "requirements", "split it", expected_hash=seen)
    assert _show(capsys, t.id)["who"] != "you"  # the agent's turn: it updates the text
    aops.set_section(t.id, "Requirements", "r, split")
    move = _show(capsys, t.id)
    assert move["who"] == "you" and move["kind"] == "approve-requirements"


def test_an_epic_child_no_longer_covered_waits_for_the_epic_reapproval(ws, aops, hops, capsys):
    e = aops.new("Epic", type="epic")
    _refine(aops, e.id, plan=None, req="the epic", ac="- [ ] all done")
    c = aops.new("child", epic=e.id)
    _refine(aops, c.id)
    hops.approve(e.id, "requirements")
    aops.claim(c.id)
    assert _show(capsys, c.id)["who"] == "agent"
    aops.set_section(c.id, "Plan", "1. something else")  # no longer what the charter covered
    move = _show(capsys, c.id)
    assert move["who"] == "you" and move["kind"] == "re-approve" and move["ref"] == "plan"
    assert move["epic"] == e.id and e.id in move["label"]
    hops.approve(e.id, "requirements")
    assert _show(capsys, c.id)["who"] == "agent"


def test_the_document_carries_the_move(ws, aops):
    t = aops.new("x")
    _refine(aops, t.id, plan=None)
    doc = ticket_document(ws, store.load(ws, t.id)[1])
    assert doc["move"]["who"] == "you" and doc["move"]["kind"] == "approve-requirements"
    assert "move" in ticket_schema()["properties"] and "move" in ticket_schema()["required"]
    assert tuple(map(int, SCHEMA_VERSION.split("."))) >= (1, 4, 0)  # `move` arrived in 1.4


def test_the_document_validates(ws, aops):
    jsonschema = pytest.importorskip("jsonschema")
    t = aops.new("x")
    doc = ticket_document(ws, store.load(ws, t.id)[1])
    jsonschema.validate(doc, ticket_schema())
