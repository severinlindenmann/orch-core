"""#174: the optional `due` date on tickets: set at creation and with `orch due`, printed by list and show, ordered
first within its priority by `orch next`, validated by `orch check`, carried by the ticket schema."""
import json
import re
from datetime import date

import jsonschema
import pytest

from orch.cli import run
from orch.core import query, store
from orch.core.constants import DUE_SOON_DAYS
from orch.core.due import due_problem, due_state, parse_due
from orch.core.events import read_events
from orch.errors import UsageError
from orch.hooks.guard import evaluate

TODAY = date(2026, 10, 6)


@pytest.fixture
def agent_env(monkeypatch, ws_root):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setenv("ORCH_SESSION", "s-1")


def _meta(ws, tid):
    return store.load(ws, tid)[1].meta


# -- the date itself -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["2026-10-31", "2028-02-29"])
def test_valid_dates(value):
    assert parse_due(value) == date.fromisoformat(value) and due_problem(value) is None


@pytest.mark.parametrize("value", ["2026-13-01", "2026-02-30", "31.10.2026", "2026-10-31T09:00Z", "20261031", 20261031, ""])
def test_invalid_dates(value):
    assert parse_due(value) is None and due_problem(value)


def test_no_due_date_is_no_problem():
    assert due_problem(None) is None and due_state({}, "open", TODAY) is None


def test_due_state_overdue_soon_and_later():
    assert due_state({"due": "2026-10-05"}, "open", TODAY) == "overdue"
    assert due_state({"due": "2026-10-06"}, "open", TODAY) == "soon"
    soon_edge = date.fromordinal(TODAY.toordinal() + DUE_SOON_DAYS).isoformat()
    later = date.fromordinal(TODAY.toordinal() + DUE_SOON_DAYS + 1).isoformat()
    assert due_state({"due": soon_edge}, "open", TODAY) == "soon"
    assert due_state({"due": later}, "open", TODAY) is None
    assert due_state({"due": "2026-10-01"}, "done", TODAY) is None  # a done ticket is never overdue
    assert due_state({"due": "not a date"}, "open", TODAY) is None


# -- setting it ----------------------------------------------------------------------------------------------------

def test_new_with_due_and_the_event(ws, aops):
    t = aops.new("Monthly expenses", due="2026-10-31")
    assert _meta(ws, t.id)["due"] == "2026-10-31"
    [created] = [e for e in read_events(ws, t.id) if e.kind == "ticket.created"]
    assert created.data["due"] == "2026-10-31"


def test_new_without_due_has_no_key(ws, aops):
    assert "due" not in _meta(ws, aops.new("plain").id)


def test_new_refuses_a_bad_date_before_creating(ws, aops):
    with pytest.raises(UsageError, match="YYYY-MM-DD|valid date"):
        aops.new("x", due="31.10.2026")
    assert store.scan(ws) == []


def test_set_and_clear_due_is_logged_and_recorded(ws, aops):
    t = aops.new("Time report")
    aops.set_due(t.id, "2026-10-09")
    ticket = store.load(ws, t.id)[1]
    assert ticket.meta["due"] == "2026-10-09" and "due 2026-10-09" in ticket.section("Log")
    aops.set_due(t.id, None)
    ticket = store.load(ws, t.id)[1]
    assert "due" not in ticket.meta and "due date cleared" in ticket.section("Log")
    edits = [e.data for e in read_events(ws, t.id) if e.kind == "ticket.edited"]
    assert edits[-2:] == [{"due": "2026-10-09"}, {"due": "none"}]
    assert all(e.actor.startswith("agent:") for e in read_events(ws, t.id) if e.kind == "ticket.edited")
    with pytest.raises(UsageError):
        aops.set_due(t.id, "2026-10-32")


def test_due_does_not_touch_an_approved_gate(ws, aops, hops):
    from orch.core.gates import gate_state
    t = aops.new("Deliverable")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    aops.set_due(t.id, "2026-10-09")
    assert gate_state(store.load(ws, t.id)[1], "requirements") == "approved"


# -- the CLI -------------------------------------------------------------------------------------------------------

def test_cli_new_due_list_show_and_clear(agent_env, capsys, ws):
    assert run(["new", "--title", "Expenses", "--due", "2026-10-31", "--json"]) == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    assert run(["list", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["due"] == "2026-10-31"
    assert run(["list"]) == 0
    assert "due 2026-10-31" in capsys.readouterr().out
    assert run(["show", tid]) == 0
    assert re.search(r"^due: '?2026-10-31'?$", capsys.readouterr().out, re.M)
    assert run(["due", tid, "2026-11-02"]) == 0
    assert capsys.readouterr().out.strip() == f"{tid}: due 2026-11-02"
    assert run(["due", tid, "--clear", "--json"]) == 0
    assert "due" not in json.loads(capsys.readouterr().out)["meta"]


def test_cli_due_needs_exactly_one_of_date_or_clear(agent_env, capsys, put):
    tid = put("backlog")
    assert run(["due", tid]) == 2
    assert run(["due", tid, "2026-10-31", "--clear"]) == 2
    assert run(["due", tid, "tomorrow"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_list_marks_overdue_tickets(agent_env, capsys, put):
    put("open", title="Late", due="2000-01-01")
    put("done", title="Late but done", due="2000-01-01")
    assert run(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any("Late " in ln and "due 2000-01-01 (overdue)" in ln for ln in lines)
    assert any("Late but done" in ln and "due 2000-01-01" in ln and "overdue" not in ln for ln in lines)


def test_agents_may_run_orch_due(ws):
    for cmd in ("orch due L-0001 2026-10-31", "orch due L-0001 --clear", "orch new --title x --due 2026-10-31"):
        assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow, cmd


# -- orch next -----------------------------------------------------------------------------------------------------

def test_next_puts_overdue_and_due_soon_first_within_a_priority(ws, put):
    plain = put("open", title="no date")
    later = put("open", title="due later", due="2026-12-01")
    soon = put("open", title="due soon", due="2026-10-08")
    overdue = put("open", title="overdue", due="2026-10-01")
    urgent = put("open", title="urgent, no date", priority="urgent")
    low_overdue = put("open", title="low but overdue", priority="low", due="2026-09-01")
    assert [e.id for e in query.next_tickets(ws, today=TODAY)] == [urgent, overdue, soon, plain, later, low_overdue]


def test_next_ignores_a_broken_due_date(ws, put):
    first = put("open", title="first")
    broken = put("open", title="broken date", due="soon")
    assert [e.id for e in query.next_tickets(ws, today=TODAY)] == [first, broken]


# -- orch check ----------------------------------------------------------------------------------------------------

def test_check_reports_an_invalid_due_date(ws, put):
    from orch.core.check import run_checks
    good = put("backlog", due="2026-10-31")
    bad = put("backlog", due="31.10.2026")
    found = {(f.ticket, f.code): f for f in run_checks(ws, emit_events=False)}
    assert (bad, "invalid-due") in found and found[(bad, "invalid-due")].level == "error"
    assert (good, "invalid-due") not in found


# -- schema --------------------------------------------------------------------------------------------------------

def test_schema_knows_due(ws, put):
    from orch.core.schema import example_document, ticket_document, ticket_from_document, ticket_schema
    schema = ticket_schema()
    assert "due" in schema["properties"]
    tid = put("backlog", due="2026-10-31")
    doc = ticket_document(ws, store.load(ws, tid)[1])
    assert doc["due"] == "2026-10-31"
    jsonschema.validate(doc, schema)
    assert ticket_from_document(doc).meta["due"] == "2026-10-31"
    assert ticket_document(ws, store.load(ws, put("backlog"))[1])["due"] is None
    assert ticket_document(ws, store.load(ws, put("backlog", due="soon"))[1])["due"] is None
    assert example_document()["due"] == "2026-10-31"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**doc, "due": "31.10.2026"}, schema)


def test_frontmatter_order_places_due_after_sprint(ws, put):
    tid = put("backlog", sprint="S1", due="2026-10-31")
    text = store.resolve(ws, tid).path.read_text(encoding="utf-8")
    assert text.index("\nsprint:") < text.index("\ndue:") < text.index("\nblocked_by:")


# -- dashboard -----------------------------------------------------------------------------------------------------

def test_board_cards_show_the_date_and_mark_overdue(dash, put):
    late = put("open", title="Late one", due="2000-01-01")
    far = put("open", title="Far one", due="2999-12-31")
    html = dash.get("/board").text
    late_card = re.search(rf'<a class="card tcard" href="/t/{late}".*?</a>', html, re.S).group(0)
    far_card = re.search(rf'<a class="card tcard" href="/t/{far}".*?</a>', html, re.S).group(0)
    assert "tc-fact-err" in late_card and "overdue · due 2000-01-01" in late_card
    assert "due 2999-12-31" in far_card and "overdue" not in far_card and "tc-fact-err" not in far_card
