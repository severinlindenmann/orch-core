"""E2: epics (type `epic`, children via `parent`), the epic charter approval and delegation."""
import json

import pytest

from orch.core import epics, ledger, sprints, store
from orch.core.gates import gate_state
from orch.errors import HumanOnlyError, TransitionError, UsageError, ValidationError


def _refine(ops, tid, *, plan="1. do it", req="r", ac="- [ ] a"):
    ops.set_section(tid, "Requirements", req)
    ops.set_section(tid, "Acceptance criteria", ac)
    if plan:
        ops.set_section(tid, "Plan", plan)
    return tid


def _epic(aops, title="Billing revamp"):
    e = aops.new(title, type="epic")
    _refine(aops, e.id, plan=None, req="the epic", ac="- [ ] all children done")
    return e.id


def _child(aops, eid, title="child", size="m", plan="1. do it"):
    c = aops.new(title, epic=eid, size=size)
    return _refine(aops, c.id, plan=plan)


def _load(ws, tid):
    return store.load(ws, tid)[1]


# -- type, creation, links -----------------------------------------------------------------------------------------

def test_epic_type_and_children(ws, aops):
    eid = _epic(aops)
    assert _load(ws, eid).meta["type"] == "epic"
    cid = _child(aops, eid)
    assert _load(ws, cid).meta["parent"] == eid
    assert [e.id for e in epics.children(ws, eid)] == [cid]


def test_no_nested_epics(ws, aops):
    eid = _epic(aops)
    with pytest.raises(ValidationError, match="nested"):
        aops.new("sub", type="epic", epic=eid)
    other = aops.new("other", type="epic")
    with pytest.raises(ValidationError, match="nested"):
        aops.link(other.id, epic=eid)


def test_new_epic_must_name_an_epic(ws, aops):
    t = aops.new("plain")
    with pytest.raises(ValidationError, match="not an epic"):
        aops.new("child", epic=t.id)


def test_link_and_unlink_epic(ws, aops):
    eid = _epic(aops)
    t = aops.new("loose")
    aops.link(t.id, epic=eid)
    assert _load(ws, t.id).meta["parent"] == eid
    aops.link(t.id, no_epic=True)
    assert _load(ws, t.id).meta["parent"] is None


def test_epics_have_no_plan_and_are_not_claimed(ws, aops, hops):
    eid = _epic(aops)
    with pytest.raises(UsageError, match="epic"):
        aops.set_section(eid, "Plan", "1. x")
    hops.approve(eid, "requirements")
    with pytest.raises(TransitionError, match="epic"):
        aops.claim(eid)
    with pytest.raises(UsageError, match="requirements"):
        hops.approve(eid, "plan")


# -- charter approval ----------------------------------------------------------------------------------------------

def test_epic_approval_covers_children(ws, aops, hops):
    eid = _epic(aops)
    c1 = _child(aops, eid, "one")
    c2 = _child(aops, eid, "two", size="xs", plan=None)
    hops.approve(eid, "requirements")
    e, t1, t2 = _load(ws, eid), _load(ws, c1), _load(ws, c2)
    assert e.status == "open" and t1.status == "open" and t2.status == "open"
    assert gate_state(t1, "requirements") == "approved" and gate_state(t1, "plan") == "approved"
    assert t1.meta["gates"]["requirements"]["epic"] == eid
    assert gate_state(t2, "plan") == "pending"  # no plan written: not covered, and xs skips it anyway
    entry = [x for x in ledger.entries(ws) if x["kind"] == "charter"][-1]
    assert entry["ticket"] == eid and entry["charter"].startswith("sha256:")
    assert [c["id"] for c in entry["children"]] == [c1, c2]
    assert entry["children"][0]["requirements"] == t1.meta["gates"]["requirements"]["hash"]
    assert entry["children"][0]["plan"] == t1.meta["gates"]["plan"]["hash"]
    assert ledger.gate_verification(ws, t1, "requirements") == "verified"
    assert ledger.gate_verification(ws, t1, "plan") == "verified"


def test_agent_works_a_covered_child_to_testing(ws, aops, hops, close_tasks):
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    aops.claim(cid)
    close_tasks(aops, cid)
    aops.set_section(cid, "Verification", "- AC1: ran it")
    aops.move(cid, "testing")
    assert _load(ws, cid).status == "testing"


def test_events_reference_the_epic_approval(ws, aops, hops):
    from orch.core.events import read_events
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    evs = [e for e in read_events(ws) if e.kind == "gate.approved"]
    child_evs = [e for e in evs if e.ticket == cid]
    assert {e.data["gate"] for e in child_evs} == {"requirements", "plan"}
    epic_ev = [e for e in evs if e.ticket == eid][-1]
    assert all(e.data["epic"] == eid and e.data["charter"] == epic_ev.data["charter"] for e in child_evs)
    assert epic_ev.data["children"] == [cid]


def test_charter_refuses_unready_or_hidden_children(ws, aops, hops):
    eid = _epic(aops)
    stub = aops.new("stub", epic=eid)
    with pytest.raises(ValidationError, match=stub.id):
        hops.approve(eid, "requirements")
    _refine(aops, stub.id, req="r‮evil")
    with pytest.raises(ValidationError, match="hidden"):
        hops.approve(eid, "requirements")


def test_epic_approval_is_human_only(ws, aops):
    eid = _epic(aops)
    with pytest.raises(HumanOnlyError):
        aops.approve(eid, "requirements")


def test_expected_charter_hash_must_match(ws, aops, hops):
    eid = _epic(aops)
    _child(aops, eid)
    shown = epics.charter(ws, _load(ws, eid))["content_hash"]
    _child(aops, eid, "late")
    with pytest.raises(ValidationError, match="changed"):
        hops.approve(eid, "requirements", expected_hash=shown)


def test_changed_child_loses_coverage(ws, aops, hops):
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    aops.claim(cid)
    aops.set_section(cid, "Plan", "1. something else")
    t = _load(ws, cid)
    assert gate_state(t, "plan") == "invalidated"
    _, ids = aops.task_add(cid, [{"text": "w"}])
    with pytest.raises(ValidationError):
        aops.task_start(cid, ids[0])
    assert epics.child_state(ws, _load(ws, eid), t) == "changed"


def test_forged_child_gate_is_not_covered(ws, aops, hops):
    """A child added after the approval, with frontmatter gates copied from the charter's form, is not covered."""
    from orch.core.gates import gate_hash
    eid = _epic(aops)
    _child(aops, eid)
    hops.approve(eid, "requirements")
    late = _child(aops, eid, "late")
    path, t = store.load(ws, late)
    for gate in ("requirements", "plan"):
        t.meta["gates"][gate] = {"approved": "2026-10-04T10:00Z", "via": "tty", "hash": gate_hash(t, gate),
                                 "hash_v": 2, "epic": eid}
    t.meta["status"] = "open"
    store.save(ws, t, path)
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(late)


def test_reparented_child_is_not_covered(ws, aops, hops):
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    other = _epic(aops, "other")
    # the human may re-parent; coverage does not travel with the ticket
    hops.link(cid, epic=other)
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(cid)


def test_agent_cannot_reparent_into_or_out_of_an_approved_epic(ws, aops, hops):
    eid = _epic(aops)
    cid = _child(aops, eid)
    loose = aops.new("loose")
    hops.approve(eid, "requirements")
    with pytest.raises(HumanOnlyError):
        aops.link(cid, no_epic=True)
    with pytest.raises(HumanOnlyError):
        aops.link(loose.id, epic=eid)


def test_epic_change_drops_coverage(ws, aops, hops):
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    aops.set_section(eid, "Out of scope", "anything mobile")
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(cid)


def test_reapprove_covers_new_children_and_shows_the_diff(ws, aops, hops):
    eid = _epic(aops)
    c1 = _child(aops, eid, "one")
    hops.approve(eid, "requirements")
    c2 = _child(aops, eid, "two")
    diff = epics.charter_diff(ws, _load(ws, eid))
    assert diff["children"] == {c1: "unchanged", c2: "new"}
    assert epics.child_state(ws, _load(ws, eid), _load(ws, c2)) == "new"
    hops.approve(eid, "requirements")
    assert _load(ws, c2).status == "open"
    aops.claim(c2)


def test_a_child_done_since_the_charter_did_not_leave_the_epic(ws, aops, hops):
    eid = _epic(aops)
    c1, c2 = _child(aops, eid, "one"), _child(aops, eid, "two")
    hops.approve(eid, "requirements")
    hops.close(c1, "done by hand")
    diff = epics.charter_diff(ws, _load(ws, eid))
    assert diff["removed"] == [] and diff["children"] == {c2: "unchanged"}  # nothing to re-approve


def test_needs_you_asks_for_the_epic_reapproval(ws, aops, hops):
    from orch.core.query import needs_you
    eid = _epic(aops)
    c1 = _child(aops, eid, "one")
    hops.approve(eid, "requirements")
    aops.claim(c1)
    aops.set_section(c1, "Plan", "1. other")
    kinds = {(i["ticket"], i["kind"]) for i in needs_you(ws)}
    assert (eid, "approve-epic") in kinds
    assert (c1, "re-approve") not in kinds  # the epic re-approval is the one decision


# -- delegation ----------------------------------------------------------------------------------------------------

def test_delegation_auto_approves_agent_children(ws, aops, hops):
    from orch.core.events import read_events
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={"max_children": 2, "max_size": "m"})
    cid = _child(aops, eid, "auto")
    aops.epic_auto_approve(cid)
    t = _load(ws, cid)
    assert t.status == "open" and gate_state(t, "requirements") == "approved" and gate_state(t, "plan") == "approved"
    assert ledger.gate_verification(ws, t, "requirements") == "delegated"
    evs = [e for e in read_events(ws, cid) if e.kind == "gate.delegated"]
    assert {e.data["gate"] for e in evs} == {"requirements", "plan"} and all(e.actor.startswith("agent:") for e in evs)
    # not signed as a human decision
    assert not [x for x in ledger.entries(ws) if x.get("ticket") == cid]
    aops.claim(cid)


def test_delegation_is_not_in_the_ticket_file(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    path = store.resolve(ws, eid).path
    assert "delegat" not in path.read_text(encoding="utf-8")
    assert epics.delegation(ws, _load(ws, eid))["max_children"] == 10


def test_delegation_limits(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={"max_children": 1, "max_size": "s"})
    big = _child(aops, eid, "big", size="m")
    with pytest.raises(ValidationError, match="size"):
        aops.epic_auto_approve(big)
    one = _child(aops, eid, "one", size="s")
    aops.epic_auto_approve(one)
    two = _child(aops, eid, "two", size="s")
    with pytest.raises(ValidationError, match="limit"):
        aops.epic_auto_approve(two)


def test_no_auto_approval_without_delegation(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements")
    cid = _child(aops, eid)
    with pytest.raises(ValidationError, match="delegation"):
        aops.epic_auto_approve(cid)


def test_agent_cannot_enable_or_widen_delegation(ws, aops, hops):
    eid = _epic(aops)
    with pytest.raises(HumanOnlyError):
        aops.approve(eid, "requirements", delegate={"max_children": 50})
    hops.approve(eid, "requirements", delegate={"max_children": 1})
    with pytest.raises(HumanOnlyError):
        aops.approve(eid, "requirements", delegate={"max_children": 50})
    assert epics.delegation(ws, _load(ws, eid))["max_children"] == 1


def test_auto_approval_is_once_per_child(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    cid = _child(aops, eid)
    aops.epic_auto_approve(cid)
    aops.claim(cid)
    aops.set_section(cid, "Plan", "1. a different plan")
    with pytest.raises((ValidationError, TransitionError)):
        aops.epic_auto_approve(cid)


def test_covered_child_cannot_be_auto_approved_after_a_change(ws, aops, hops):
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements", delegate={})
    aops.claim(cid)
    aops.set_section(cid, "Requirements", "r, now bigger")
    with pytest.raises((ValidationError, TransitionError)):
        aops.epic_auto_approve(cid)


def test_pause_stops_further_auto_approvals_only(ws, aops, hops):
    """Owner decision: a pause stops new auto-approvals; the children auto-approved before it stay approved (the
    signed pause entry keeps them with their hashes)."""
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    before = _child(aops, eid, "before")
    aops.epic_auto_approve(before)
    with pytest.raises(HumanOnlyError):
        aops.epic_pause(eid)
    hops.epic_pause(eid)
    d = epics.delegation(ws, _load(ws, eid))
    assert d["active"] is False and d["paused"] and [k["id"] for k in d["kept"]] == [before]
    after = _child(aops, eid, "after")
    with pytest.raises(ValidationError, match="paused"):
        aops.epic_auto_approve(after)
    aops.claim(before)  # still approved
    assert epics.child_state(ws, _load(ws, eid), _load(ws, before)) == "delegated"
    pause = [x for x in ledger.entries(ws) if x["kind"] == "pause"][-1]
    t = _load(ws, before)
    assert pause["kept"] == [{"id": before, "requirements": t.meta["gates"]["requirements"]["hash"],
                              "plan": t.meta["gates"]["plan"]["hash"]}]


def test_a_kept_child_with_a_forged_new_hash_is_refused_after_the_pause(ws, aops, hops):
    from orch.core.events import Actor, append_event
    from orch.core.gates import gate_hash
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    cid = _child(aops, eid)
    aops.epic_auto_approve(cid)
    did = epics.delegation(ws, _load(ws, eid))["id"]
    hops.epic_pause(eid)
    aops.claim(cid)
    _, ids = aops.task_add(cid, [{"text": "w"}])
    aops.set_section(cid, "Plan", "1. a bigger plan")
    path, t = store.load(ws, cid)
    h = gate_hash(t, "plan")
    t.meta["gates"]["plan"] = {**t.meta["gates"]["plan"], "hash": h}
    store.save(ws, t, path)
    append_event(ws, cid, "gate.delegated", Actor("agent", "x", "cli"),
                 {"gate": "plan", "hash": h, "epic": eid, "delegation": did})
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.task_start(cid, ids[0])


def test_human_created_children_are_not_auto_approved(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    c = hops.new("the human's child", epic=eid)
    _refine(aops, c.id)
    with pytest.raises(ValidationError, match="created by the human"):
        aops.epic_auto_approve(c.id)


def test_frontmatter_delegations_count_toward_the_limit(ws, aops, hops):
    """A child whose frontmatter names the delegation counts even without its event."""
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={"max_children": 1})
    did = epics.delegation(ws, _load(ws, eid))["id"]
    forged = _child(aops, eid, "forged")
    path, t = store.load(ws, forged)
    t.meta["gates"]["requirements"] = {"approved": "2026-10-04T10:00Z", "via": "cli", "hash": "sha256:" + "1" * 64,
                                       "delegation": did}
    store.save(ws, t, path)
    real = _child(aops, eid, "real")
    with pytest.raises(ValidationError, match="limit"):
        aops.epic_auto_approve(real)


def test_hidden_characters_after_auto_approval_stop_the_agent(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    cid = _child(aops, eid)
    aops.epic_auto_approve(cid)
    path, t = store.load(ws, cid)
    t.meta["title"] = "child \u202e reversed"  # the title is not hashed, but it is checked again at proceed time
    store.save(ws, t, path)
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(cid)


def test_epic_change_pauses_delegation(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    aops.set_section(eid, "Requirements", "the epic, but wider")
    cid = _child(aops, eid)
    with pytest.raises(ValidationError, match="epic"):
        aops.epic_auto_approve(cid)


def test_forged_delegated_gate_is_refused(ws, aops, hops):
    """Frontmatter that claims a delegated approval without the event (or with an event naming another
    delegation) does not let the agent proceed."""
    from orch.core.gates import gate_hash
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    did = epics.delegation(ws, _load(ws, eid))["id"]
    cid = _child(aops, eid)
    path, t = store.load(ws, cid)
    for gate in ("requirements", "plan"):
        t.meta["gates"][gate] = {"approved": "2026-10-04T10:00Z", "via": "cli", "hash": gate_hash(t, gate),
                                 "hash_v": 2, "epic": eid, "delegation": did}
    t.meta["status"] = "open"
    store.save(ws, t, path)
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(cid)
    from orch.core.events import Actor, append_event
    for gate in ("requirements", "plan"):
        append_event(ws, cid, "gate.delegated", Actor("agent", "x", "cli"),
                     {"gate": gate, "hash": t.meta["gates"][gate]["hash"], "epic": eid, "delegation": "sha256:" + "0" * 64})
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(cid)


def test_forged_delegated_gate_beyond_the_size_limit_is_refused(ws, aops, hops):
    from orch.core.events import Actor, append_event
    from orch.core.gates import gate_hash
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={"max_size": "s"})
    did = epics.delegation(ws, _load(ws, eid))["id"]
    cid = _child(aops, eid, size="l")
    path, t = store.load(ws, cid)
    for gate in ("requirements", "plan"):
        h = gate_hash(t, gate)
        t.meta["gates"][gate] = {"approved": "2026-10-04T10:00Z", "via": "cli", "hash": h, "hash_v": 2, "epic": eid,
                                 "delegation": did}
        append_event(ws, cid, "gate.delegated", Actor("agent", "x", "cli"),
                     {"gate": gate, "hash": h, "epic": eid, "delegation": did})
    t.meta["status"] = "open"
    store.save(ws, t, path)
    with pytest.raises(ValidationError, match="not in the ledger"):
        aops.claim(cid)


# -- verdict -------------------------------------------------------------------------------------------------------

def _to_testing(aops, cid, close_tasks):
    aops.claim(cid)
    close_tasks(aops, cid)
    aops.set_section(cid, "Verification", "- AC1: ran it")
    aops.move(cid, "testing")


def test_epic_verdict_closes_children_in_testing(ws, aops, hops, close_tasks):
    eid = _epic(aops)
    c1, c2 = _child(aops, eid, "one"), _child(aops, eid, "two")
    hops.approve(eid, "requirements")
    _to_testing(aops, c1, close_tasks)
    with pytest.raises(ValidationError, match="testing"):
        hops.verdict(eid, "done")
    _to_testing(aops, c2, close_tasks)
    hops.verdict(eid, "done")
    assert {_load(ws, x).status for x in (eid, c1, c2)} == {"done"}
    kinds = [(x["ticket"], x["kind"]) for x in ledger.entries(ws) if x["kind"] == "verdict"]
    assert {(c1, "verdict"), (c2, "verdict"), (eid, "verdict")} <= set(kinds)
    from orch.core.check import run_checks
    assert not [f for f in run_checks(ws, emit_events=False) if f.level == "error" and f.ticket in (eid, c1, c2)]


def test_epic_verdict_is_human_only_and_never_automatic(ws, aops, hops, close_tasks):
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    _to_testing(aops, cid, close_tasks)
    assert _load(ws, eid).status == "open"
    with pytest.raises(HumanOnlyError):
        aops.verdict(eid, "done")


# -- check -------------------------------------------------------------------------------------------------------

def test_check_audits_delegated_approvals(ws, aops, hops):
    from orch.core.check import run_checks
    eid = _epic(aops)
    c1 = _child(aops, eid)
    hops.approve(eid, "requirements", delegate={})
    cid = _child(aops, eid, "auto")
    aops.epic_auto_approve(cid)
    found = {(f.ticket, f.code): f.level for f in run_checks(ws, emit_events=False)}
    assert found.get((cid, "delegated-approval")) == "info"
    assert (cid, "unverified-approval") not in found and (c1, "unverified-approval") not in found
    hops.epic_pause(eid)  # kept: still a valid auto-approval
    found = {(f.ticket, f.code): f.level for f in run_checks(ws, emit_events=False)}
    assert found.get((cid, "delegated-approval")) == "info"
    aops.set_section(eid, "Requirements", "the epic, changed")  # suspends the delegation
    found = {(f.ticket, f.code): f.level for f in run_checks(ws, emit_events=False)}
    assert found.get((cid, "unsigned-decision")) == "warning"


# -- sprints -----------------------------------------------------------------------------------------------------

def test_sprints(configure, aops):
    ws = configure(sprints=[{"id": "S1", "name": "Sprint 1", "start": "2026-10-01", "end": "2026-10-14"},
                            {"id": "S2", "name": "Sprint 2", "start": "2026-10-15", "end": "2026-10-28"}])
    from orch.core.events import Actor
    from orch.core.ops import Ops
    ops = Ops(ws, Actor("agent", "claude-code", "cli", "7f3c9a21-0000"))
    t = ops.new("planned", sprint="S1")
    assert _load(ws, t.id).meta["sprint"] == "S1"
    ops.link(t.id, sprint="S2")
    assert _load(ws, t.id).meta["sprint"] == "S2"
    with pytest.raises(ValidationError, match="sprint"):
        ops.link(t.id, sprint="S9")
    assert sprints.current(ws, today="2026-10-20")["id"] == "S2"
    assert sprints.current(ws, today="2026-11-20") is None


# -- guard: `parent` is protected once the epic is approved -----------------------------------------------------

def _edit(ws, tid, old, new):
    from orch.hooks.guard import evaluate
    path = store.resolve(ws, tid).path
    return evaluate(ws, {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": old,
                                                             "new_string": new}})


def test_guard_protects_parent_after_epic_approval(ws, aops, hops):
    eid = _epic(aops)
    cid = _child(aops, eid)
    loose = aops.new("loose").id
    other = _epic(aops, "other")
    assert _edit(ws, loose, "parent: null", f"parent: {other}").allow  # an unapproved epic: still planning
    hops.approve(eid, "requirements")
    d = _edit(ws, cid, f"parent: {eid}", "parent: null")
    assert not d.allow and "parent" in d.reason
    d = _edit(ws, loose, "parent: null", f"parent: {eid}")
    assert not d.allow and "parent" in d.reason


@pytest.mark.parametrize("cmd", ["orch epic pause L-0001", "uv run orch --json epic pause L-0001",
                                 "o''rch epic pause L-0001"])
def test_guard_keeps_epic_pause_with_the_human(ws, cmd):
    from orch.hooks.guard import evaluate
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow


@pytest.mark.parametrize("cmd", ["orch epic show L-0001", "orch epic auto-approve L-0002", "orch sprint list"])
def test_guard_allows_agent_epic_commands(ws, cmd):
    from orch.hooks.guard import evaluate
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow


def test_editing_a_covered_childs_requirements_stops_the_agent(ws, aops, hops, close_tasks):
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    aops.claim(cid)
    close_tasks(aops, cid)
    aops.set_section(cid, "Verification", "- AC1: ran it")
    aops.set_section(cid, "Out of scope", "nothing else")  # widens what was approved
    assert gate_state(_load(ws, cid), "requirements") == "invalidated"
    from orch.core.query import needs_you
    assert (eid, "approve-epic") in {(i["ticket"], i["kind"]) for i in needs_you(ws)}
    hops.approve(eid, "requirements")  # the human re-approves the epic: covered again
    assert ledger.gate_verification(ws, _load(ws, cid), "requirements") == "verified"
    aops.move(cid, "testing")


def test_stale_epic_verdict_is_refused(ws, aops, hops, close_tasks):
    eid = _epic(aops)
    c1, c2 = _child(aops, eid, "one"), _child(aops, eid, "two")
    hops.approve(eid, "requirements")
    _to_testing(aops, c1, close_tasks)
    _to_testing(aops, c2, close_tasks)
    seen = epics.verdict_hash(epics.open_children(ws, _load(ws, eid)), ws)
    aops.set_section(c2, "Verification", "- AC1: ran it again, differently")
    with pytest.raises(ValidationError, match="changed"):
        hops.verdict(eid, "done", expected_hash=seen)
    assert {_load(ws, x).status for x in (c1, c2)} == {"testing"}


def test_epic_verdict_never_closes_part_of_the_children(ws, aops, hops, close_tasks):
    eid = _epic(aops)
    c1, c2 = _child(aops, eid, "one"), _child(aops, eid, "two")
    hops.approve(eid, "requirements")
    _to_testing(aops, c1, close_tasks)
    _to_testing(aops, c2, close_tasks)
    aops.set_section(c2, "Verification", "- AC1: ran it ​")  # hidden character in the second child only
    with pytest.raises(ValidationError, match="hidden"):
        hops.verdict(eid, "done")
    assert {_load(ws, x).status for x in (eid, c1, c2)} == {"open", "testing"}
    assert _load(ws, c1).status == "testing"


def test_a_second_created_event_does_not_make_a_human_child_an_agents(ws, aops, hops):
    from orch.core.events import Actor, append_event, read_events
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    c = hops.new("the human's child", epic=eid)
    _refine(aops, c.id)
    append_event(ws, c.id, "ticket.created", Actor("agent", "x", "cli"), {"title": "x"})
    assert not epics.created_by_agent(read_events(ws), c.id)
    with pytest.raises(ValidationError, match="created by the human"):
        aops.epic_auto_approve(c.id)
    agent_child = _child(aops, eid, "agent's")
    append_event(ws, agent_child, "ticket.created", Actor("agent", "x", "cli"), {"title": "x"})
    assert not epics.created_by_agent(read_events(ws), agent_child)  # two "created" events: refused either way


def test_a_changed_epic_child_is_refused_until_the_epic_is_re_approved(ws, aops, hops, close_tasks):
    """Final review C1: the charter covers the child's hashes as signed; once the child's text changes the agent
    stops (task work and testing) until the human re-approves the epic."""
    eid = _epic(aops)
    cid = _child(aops, eid)
    hops.approve(eid, "requirements")
    aops.claim(cid)
    _, ids = aops.task_add(cid, [{"text": "w"}, {"text": "v"}])
    aops.task_start(cid, ids[0])
    aops.task_done(cid, ids[0])
    aops.set_section(cid, "Verification", "- AC1: ran it")
    aops.set_section(cid, "Requirements", "r, and more")
    with pytest.raises(ValidationError, match="changed since"):
        aops.task_start(cid, ids[1])
    aops.task_skip(cid, ids[1], "not needed")
    with pytest.raises(ValidationError, match="changed since"):
        aops.move(cid, "testing")
    hops.approve(eid, "requirements")
    aops.move(cid, "testing")
    assert _load(ws, cid).status == "testing"


def test_rules_name_the_epic_and_ledger_steps_that_are_the_humans(ws):
    """Final review M1: `orch rules` lists the human-only epic and ledger steps and mentions epics and delegation."""
    from orch.core.rules import render_rules
    rules = render_rules(ws.config)
    human_only = next(line for line in rules.split("\n") if line.startswith("human-only:"))
    for step in ("orch epic pause", "orch ledger adopt", "moving a ticket into or out of an approved epic"):
        assert step in human_only, step
    epics_line = next(line for line in rules.split("\n") if line.startswith("epics:"))
    assert "orch epic auto-approve" in epics_line and "delegation" in epics_line
    asks = next(line for line in rules.split("\n") if line.startswith("ticket file:"))
    assert "--despite-open-question" in asks


def test_auto_approval_refuses_hidden_characters(ws, aops, hops):
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={})
    cid = _child(aops, eid, plan="1. do it&#x202E;")
    with pytest.raises(ValidationError, match="hidden or control characters"):
        aops.epic_auto_approve(cid)


def test_a_child_edited_between_the_epic_check_and_its_close_closes_nothing(ws, aops, hops, close_tasks):
    """Re-review (a): each child's verdict is bound to its own hash and every child is re-read under its lock before
    any is written, so an edit after the epic's hash check closes no child (no partial close)."""
    eid = _epic(aops)
    c1, c2 = _child(aops, eid, "one"), _child(aops, eid, "two")
    hops.approve(eid, "requirements")
    _to_testing(aops, c1, close_tasks)
    _to_testing(aops, c2, close_tasks)
    original = hops._close_children

    def edited_meanwhile(ids, seen, note):
        aops.set_section(c2, "Verification", "- AC1: something else")
        return original(ids, seen, note)
    hops._close_children = edited_meanwhile
    with pytest.raises(ValidationError, match=f"{c2} changed since you read them; no child was closed"):
        hops.verdict(eid, "done")
    assert [_load(ws, c).status for c in (c1, c2, eid)] == ["testing", "testing", "open"]
    assert not [e for e in ledger.entries(ws) if e["kind"] == "verdict"]
    del hops._close_children
    hops.verdict(eid, "done")
    assert [_load(ws, c).status for c in (c1, c2, eid)] == ["done", "done", "done"]
    child_entries = {e["ticket"]: e for e in ledger.entries(ws) if e["kind"] == "verdict" and e["ticket"] != eid}
    assert set(child_entries) == {c1, c2} and all(e["verdict_hash"] for e in child_entries.values())


# -- approving the children's plans in one confirmation (#27) ------------------------------------------------------

def _claimed_with_plans(ws, aops, hops, n=3):
    """An approved epic whose `n` children were claimed after the approval and then got their plans."""
    eid = _epic(aops)
    kids = [_child(aops, eid, f"child {i}", plan=None) for i in range(n)]
    hops.approve(eid, "requirements")
    for cid in kids:
        aops.claim(cid)
        aops.set_section(cid, "Plan", f"1. build {cid}")
    return eid, kids


def _plans_seen(ws, eid):
    from orch.core.gates import gate_hash
    return {t.id: gate_hash(t, "plan") for t in epics.pending_plans(ws, _load(ws, eid))}


def test_pending_plans_lists_in_progress_children_waiting_for_plan_approval(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops)
    assert [t.id for t in epics.pending_plans(ws, _load(ws, eid))] == kids
    hops.approve(kids[0], "plan")
    assert [t.id for t in epics.pending_plans(ws, _load(ws, eid))] == kids[1:]


def test_one_confirmation_signs_one_entry_per_child_plan(ws, aops, hops):
    from orch.core.events import read_events
    eid, kids = _claimed_with_plans(ws, aops, hops)
    seen = _plans_seen(ws, eid)
    approved, skipped = hops.approve_plans(eid, seen)
    assert [t.id for t in approved] == kids and skipped == []
    signed = [e for e in ledger.entries(ws) if e["kind"] == "gate" and e.get("gate") == "plan"]
    assert {e["ticket"]: e["hash"] for e in signed} == seen  # each bound to its own child's plan text
    evs = {e.ticket: e.data["hash"] for e in read_events(ws) if e.kind == "gate.approved" and e.data["gate"] == "plan"}
    assert evs == seen
    for cid in kids:
        t = _load(ws, cid)
        assert gate_state(t, "plan") == "approved" and ledger.gate_verification(ws, t, "plan") == "verified"


def test_a_plan_changed_after_listing_is_skipped_and_reported(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops)
    seen = _plans_seen(ws, eid)
    aops.set_section(kids[1], "Plan", "1. something else")
    approved, skipped = hops.approve_plans(eid, seen)
    assert [t.id for t in approved] == [kids[0], kids[2]]
    assert [s[0] for s in skipped] == [kids[1]] and "changed" in skipped[0][1]
    assert gate_state(_load(ws, kids[1]), "plan") == "pending"


def test_batch_plan_approval_is_human_only(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops)
    with pytest.raises(HumanOnlyError):
        aops.approve_plans(eid, _plans_seen(ws, eid))
    assert all(gate_state(_load(ws, c), "plan") == "pending" for c in kids)


def test_hashes_bind_per_child(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops, n=2)
    seen = _plans_seen(ws, eid)
    swapped = {kids[0]: seen[kids[1]], kids[1]: seen[kids[0]]}
    approved, skipped = hops.approve_plans(eid, swapped)
    assert approved == [] and [s[0] for s in skipped] == kids


def test_a_later_plan_edit_invalidates_only_that_child(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops)
    hops.approve_plans(eid, _plans_seen(ws, eid))
    aops.set_section(kids[2], "Plan", "1. changed later")
    states = [gate_state(_load(ws, c), "plan") for c in kids]
    assert states == ["approved", "approved", "invalidated"]
    assert [t.id for t in epics.pending_plans(ws, _load(ws, eid))] == [kids[2]]


def test_batch_plan_approval_refuses_non_children_and_empty_lists(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops, n=1)
    loose = aops.new("loose")
    _refine(aops, loose.id, plan=None)
    hops.approve(loose.id, "requirements")
    aops.claim(loose.id)
    aops.set_section(loose.id, "Plan", "1. x")
    from orch.core.gates import gate_hash
    approved, skipped = hops.approve_plans(eid, {loose.id: gate_hash(_load(ws, loose.id), "plan")})
    assert approved == [] and skipped[0][0] == loose.id and "not a child" in skipped[0][1]
    with pytest.raises(ValidationError):
        hops.approve_plans(eid, {})
    with pytest.raises(UsageError, match="not an epic"):
        hops.approve_plans(loose.id, {kids[0]: "sha256:x"})


def test_dry_run_approves_nothing(ws, aops, human):
    from conftest import human_ops
    hops = human_ops(ws, human)
    eid, kids = _claimed_with_plans(ws, aops, hops, n=2)
    approved, skipped = human_ops(ws, human, dry_run=True).approve_plans(eid, _plans_seen(ws, eid))
    assert [t.id for t in approved] == kids and skipped == []
    assert all(gate_state(_load(ws, c), "plan") == "pending" for c in kids)
    assert not [e for e in ledger.entries(ws) if e.get("gate") == "plan"]


def test_duplicate_keys_are_refused(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops, n=1)
    h = _plans_seen(ws, eid)[kids[0]]
    with pytest.raises(UsageError, match="more than once"):
        hops.approve_plans(eid, {kids[0]: h, kids[0].lower(): h})
    assert gate_state(_load(ws, kids[0]), "plan") == "pending"


def test_a_plan_already_approved_for_that_hash_is_skipped(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops, n=2)
    seen = _plans_seen(ws, eid)
    hops.approve(kids[0], "plan")
    before = len(ledger.entries(ws))
    approved, skipped = hops.approve_plans(eid, seen)
    assert [t.id for t in approved] == [kids[1]] and skipped == [(kids[0], "already approved")]
    assert len(ledger.entries(ws)) == before + 1  # only the second child was signed


def test_open_question_lines_are_waived_per_child(ws, aops, hops):
    eid, kids = _claimed_with_plans(ws, aops, hops, n=2)
    for cid in kids:
        aops.set_section(cid, "Plan", f"1. build {cid}\nOpen question: which queue for {cid}?")
    approved, skipped = hops.approve_plans(eid, _plans_seen(ws, eid), despite=[kids[0].lower()])
    assert [t.id for t in approved] == [kids[0]]
    assert [s[0] for s in skipped] == [kids[1]] and "open question" in skipped[0][1]
    entry = [e for e in ledger.entries(ws) if e.get("gate") == "plan"][-1]
    assert entry["ticket"] == kids[0] and entry.get("despite_open_question") is True


_BATCH_PROC = """
import json, os, sys, time
from pathlib import Path
import orch.actor
orch.actor.process_chain = lambda: []  # as conftest: the suite's real process tree must not decide who acts
from orch.core.events import Actor
from orch.core.ops import Ops
from orch.core.workspace import Workspace
root, go, seen = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
ops = Ops(Workspace.open(Path(root)), Actor("human", "you", "tty"))
while not os.path.exists(go):
    time.sleep(0.001)
approved, skipped = ops.approve_plans(sys.argv[4], seen)
print(json.dumps([len(approved), len(skipped)]))
"""


def test_two_concurrent_batches_sign_each_child_once(ws, ws_root, aops, hops, tmp_path):
    """#49: the "already approved" check is made under the ticket lock, so two batches started at the same
    instant never sign one child twice."""
    import subprocess
    import sys
    eid, kids = _claimed_with_plans(ws, aops, hops, n=12)
    seen = _plans_seen(ws, eid)
    go = tmp_path / "go"
    args = [sys.executable, "-c", _BATCH_PROC, str(ws_root), str(go), json.dumps(seen), eid]
    procs = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    go.write_text("", encoding="utf-8")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        results.append(json.loads(out))
    assert sum(r[0] for r in results) == len(kids)  # each child approved by exactly one batch
    signed = [e["ticket"] for e in ledger.entries(ws) if e["kind"] == "gate" and e.get("gate") == "plan"]
    assert sorted(signed) == sorted(kids)
    assert all(gate_state(_load(ws, c), "plan") == "approved" for c in kids)
