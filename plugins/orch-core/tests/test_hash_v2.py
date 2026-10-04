"""#22: requirements hash v2 binds size and type (and a non-empty Summary); v1 approvals stay valid; approvals are
refused while the gated text still asks the human something or a blocking question is open."""
import hashlib

import pytest

from orch.core.gates import (HASH_VERSION, gate_hash, gate_parts, gate_state, human_questions_in, normalized_text,
                             record_approval)
from orch.core.model import new_ticket
from orch.errors import ValidationError


def _t(size="m"):
    t = new_ticket("L-0001", "x", type="feature", priority="normal", size=size, created="2026-09-30T08:00Z")
    t.set_section("Requirements", "Must back up.")
    t.set_section("Acceptance criteria", "- [ ] nightly job")
    t.set_section("Plan", "1. do it")
    return t


def _v1_hash(t) -> str:
    """The pre-v2 requirements hash of `_t()`, spelled out by hand so a change to v1 is caught."""
    text = "## Requirements\n\nMust back up.\n\n## Acceptance criteria\n\n- [ ] nightly job\n\n## Out of scope\n\n"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_v1_pinned_golden_hash():
    # Golden value computed with the code before hash v2 existed: existing approvals must keep matching.
    t = _t()
    assert gate_hash(t, "requirements", version=1) == _v1_hash(t)


def test_v2_covers_size_and_type():
    a, b, c = _t("m"), _t("xs"), _t("m")
    c.meta["type"] = "bug"
    assert HASH_VERSION == 3  # v3 (inline artifacts) equals v2 when the text shows none
    assert gate_hash(a, "requirements") != gate_hash(b, "requirements")
    assert gate_hash(a, "requirements") != gate_hash(c, "requirements")
    assert "size: m" in normalized_text(a, "requirements") and "type: feature" in normalized_text(a, "requirements")


def test_v2_summary_only_when_present():
    t = _t()
    before = gate_hash(t, "requirements")
    t.set_section("Summary", "")
    assert gate_hash(t, "requirements") == before
    t.set_section("Summary", "- backs up nightly")
    assert gate_hash(t, "requirements") != before
    assert [name for name, _ in gate_parts(t, "requirements")][0] == "Summary"


def test_new_approval_records_v2_and_size_change_invalidates(ws, human):
    t = _t()
    record_approval(ws, t, "requirements", human)
    assert t.meta["gates"]["requirements"]["hash_v"] == HASH_VERSION
    assert gate_state(t, "requirements") == "approved"
    t.meta["size"] = "xs"
    assert gate_state(t, "requirements") == "invalidated"


def test_type_change_invalidates(ws, human):
    t = _t()
    record_approval(ws, t, "requirements", human)
    t.meta["type"] = "chore"
    assert gate_state(t, "requirements") == "invalidated"


def test_v1_approval_stays_valid():
    t = _t()
    t.meta["gates"]["requirements"] = {"approved": "2026-09-30T08:00Z", "via": "tty", "hash": _v1_hash(t)}
    assert gate_state(t, "requirements") == "approved"
    t.set_section("Requirements", "Must back up twice.")
    assert gate_state(t, "requirements") == "invalidated"


def test_plan_hash_has_no_meta_lines():
    t = _t()
    assert normalized_text(t, "plan") == "## Plan\n\n1. do it"


@pytest.mark.parametrize("line", [
    "3. Open question for the human: do we want a hard coverage gate now?",
    "- Question for you: which region?",
    "Coverage threshold: TBD",
    "**Open question:** keep the old job?",
    "- TBD",
    "TBD: decide the region",
    "Open questions? none yet",
])
def test_human_question_marker_found(line):
    t = _t()
    t.set_section("Plan", f"1. do it\n{line}\n2. ship")
    assert human_questions_in(t, "plan")


@pytest.mark.parametrize("text", [
    "1. Answer the question in the README\n2. questions go through orch ask",
    "1. Fix the tbd_parser module",
    "1. Show open questions on the Today page",
    "No open questions.",
    "1. Fix the TBD parser so it reads TBD values",
    "1. List the questions for the human in the summary panel",
])
def test_human_question_marker_ignores_plain_text(text):
    t = _t()
    t.set_section("Plan", text)
    assert human_questions_in(t, "plan") == []


def test_approve_refuses_plan_with_open_question(working, aops, hops):
    aops.set_section(working, "Plan", "1. do it\n2. Open question for the human: which percentage?")
    with pytest.raises(ValidationError, match="open question for you"):
        hops.approve(working, "plan")


def test_approve_refuses_requirements_with_open_question(ws, aops, hops):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "Must back up.\nRetention: TBD")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    with pytest.raises(ValidationError, match="open question for you"):
        hops.approve(t.id, "requirements")


def test_approve_plan_refused_while_blocking_question_open(working, aops, hops, tmp_path):
    aops.set_section(working, "Plan", "1. do it")
    ask = tmp_path / "q.yaml"
    ask.write_text("questions:\n  - text: Which region?\n    options: [{key: a, label: A}, {key: b, label: B}]\n"
                   "    recommended: a\n    blocking: true\n", encoding="utf-8")
    from orch.core.questions import parse_ask_file
    aops.ask(working, parse_ask_file(ask.read_text(encoding="utf-8")))
    with pytest.raises(ValidationError, match="blocking questions open"):
        hops.approve(working, "plan")


def test_dashboard_offers_no_approve_for_plan_with_question(ws, working, aops):
    from orch.core import store
    from orch.dashboard.data.steps import can_approve
    aops.set_section(working, "Plan", "1. Open question for the human: A or B?")
    t = store.load(ws, working)[1]
    assert not can_approve(t, "plan")


def test_guard_denies_size_and_type_edits_after_requirements_approval(ws, aops, hops):
    from orch.core import store
    from orch.hooks.guard import evaluate
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    path = store.resolve(ws, t.id).path
    before = {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "size: m",
                                                   "new_string": "size: xs"}}
    assert evaluate(ws, before).allow  # still in refinement: the agent sizes the ticket
    hops.approve(t.id, "requirements")
    path = store.resolve(ws, t.id).path
    d = evaluate(ws, {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "size: m",
                                                           "new_string": "size: xs"}})
    assert not d.allow and "size" in d.reason
    d = evaluate(ws, {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "type: feature",
                                                           "new_string": "type: chore"}})
    assert not d.allow and "type" in d.reason


def test_raw_edit_by_human_may_change_size_and_it_invalidates(ws, aops, hops):
    from orch.core import gates, store
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    path = store.resolve(ws, t.id).path
    hops.replace_raw(t.id, path.read_text(encoding="utf-8").replace("size: m", "size: l"))
    assert gates.gate_state(store.load(ws, t.id)[1], "requirements") == "invalidated"


def test_approval_event_records_hash_version(ws, aops, hops):
    from orch.core.events import read_events
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    ev = [e for e in read_events(ws, t.id) if e.kind == "gate.approved"][-1]
    assert ev.data["hash_v"] == HASH_VERSION


def test_schema_document_lists_what_the_gate_covers(ws, aops):
    from orch.core import store
    from orch.core.schema import ticket_document
    t = aops.new("x")
    doc = ticket_document(ws, store.load(ws, t.id)[1])
    assert doc["gates"]["requirements"]["covers"] == ["Requirements", "Acceptance criteria", "Out of scope", "size",
                                                      "type"]
    assert doc["gates"]["plan"]["covers"] == ["Plan"]


def test_hint_names_the_open_question(ws, working, aops):
    from orch.core import store
    from orch.dashboard.data.steps import reapprove_hint
    aops.set_section(working, "Plan", "1. Open question for the human: A or B?")
    hint = reapprove_hint(store.load(ws, working)[1], "plan", [])
    assert "reads as an open question for you" in hint["text"] and "orch ask" in hint["text"]
    assert "box ticked" in hint["text"]


def test_a_feature_about_open_questions_is_approvable(ws, aops, hops):
    t = aops.new("Show open questions on the Today page")
    aops.set_section(t.id, "Requirements", "Show open questions on the Today page, newest first.")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] open questions for the human are listed")
    hops.approve(t.id, "requirements")


def test_cli_override_flag(ws, aops, monkeypatch, capsys):
    from orch import actor
    from orch.cli import run
    from orch.core import store
    from orch.core.events import read_events
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r\nRetention: TBD")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": t.id)
    assert run(["approve", t.id, "requirements"]) == 5
    assert "--despite-open-question" in capsys.readouterr().err
    assert run(["approve", t.id, "requirements", "--despite-open-question"]) == 0
    assert store.load(ws, t.id)[1].status == "open"
    assert [e for e in read_events(ws, t.id) if e.kind == "gate.approved"][-1].data["despite_open_question"]


def test_dashboard_override_checkbox(dash, ws, aops):
    from orch.core import gates, store
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r\nOpen question: which region?")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    page = dash.get("/groom").text
    assert 'name="despite_open_question"' in page and "which region?" in page
    assert 'name="despite_open_question" value="1" required' in dash.get(f"/t/{t.id}").text
    seen = gates.gate_hash(store.load(ws, t.id)[1], "requirements")
    r = dash.post(f"/t/{t.id}/approve", data={"gate": "requirements", "seen": seen}, follow_redirects=False)
    assert "open+question" in r.headers["location"]
    dash.post(f"/t/{t.id}/approve", data={"gate": "requirements", "seen": seen, "despite_open_question": "1"},
              follow_redirects=False)
    assert store.load(ws, t.id)[1].status == "open"
