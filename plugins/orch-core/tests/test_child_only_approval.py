"""#209 part B: a child added to an open, approved epic is approved on its own (`orch approve <child>`), showing and
binding only that child. The epic's charter and the existing children are not touched, re-approved or re-stamped, and
the child's own signed gate entry is what lets an agent work it. Human only, like every approval."""
import pytest

from orch import actor
from orch.cli import run
from orch.core import epics, ledger, store
from orch.core.events import read_events
from orch.core.gates import gate_hash, gate_state
from orch.core.query import needs_you
from orch.errors import HumanOnlyError

from test_epics import _child, _epic, _load


@pytest.fixture
def approved_epic(ws, aops, hops):
    eid = _epic(aops)
    old = _child(aops, eid, "old")
    hops.approve(eid, "requirements")
    new = _child(aops, eid, "new")  # added after the epic was approved
    return eid, old, new


def _stamps(ws, tid):
    return [(e.kind, e.data.get("gate")) for e in read_events(ws, tid) if e.kind == "gate.approved"]


def test_a_new_child_is_approved_without_the_epic(ws, hops, aops, approved_epic):
    eid, old, new = approved_epic
    assert epics.child_state(ws, _load(ws, eid), _load(ws, new)) == "new"
    charters_before = [e for e in ledger.entries(ws) if e["kind"] == "charter"]
    old_before, epic_before = _stamps(ws, old), _stamps(ws, eid)
    old_file = store.resolve(ws, old).path.read_text(encoding="utf-8")

    t = _load(ws, new)
    hops.approve_together(new, requirements_hash=gate_hash(t, "requirements"), plan_hash=gate_hash(t, "plan"))

    t = _load(ws, new)
    assert t.status == "open" and gate_state(t, "requirements") == "approved" and gate_state(t, "plan") == "approved"
    assert "epic" not in t.meta["gates"]["requirements"]  # its own approval, not the charter's
    assert ledger.gate_verification(ws, t, "requirements") == "verified"
    assert ledger.gate_verification(ws, t, "plan") == "verified"
    assert ledger._charter_current(ws, t, "requirements", ledger.entries(ws))
    assert epics.child_state(ws, _load(ws, eid), t) == "approved"
    # the existing child and the epic are not re-stamped, re-approved or rewritten
    assert _stamps(ws, old) == old_before and _stamps(ws, eid) == epic_before
    assert store.resolve(ws, old).path.read_text(encoding="utf-8") == old_file
    assert [e for e in ledger.entries(ws) if e["kind"] == "charter"] == charters_before
    # and the agent works it: no whole-epic re-approval is needed
    ledger.require_signed(ws, t, ("requirements", "plan"))
    aops.claim(new)


def test_the_epic_no_longer_asks_for_a_child_approved_on_its_own(ws, hops, approved_epic):
    eid, old, new = approved_epic
    assert (eid, "approve-epic") in {(i["ticket"], i["kind"]) for i in needs_you(ws)}
    t = _load(ws, new)
    hops.approve_together(new, requirements_hash=gate_hash(t, "requirements"), plan_hash=gate_hash(t, "plan"))
    assert (eid, "approve-epic") not in {(i["ticket"], i["kind"]) for i in needs_you(ws)}
    assert (new, "approve-requirements") not in {(i["ticket"], i["kind"]) for i in needs_you(ws)}


def test_a_later_epic_reapproval_does_not_restamp_the_child_either(ws, hops, aops, approved_epic):
    eid, old, new = approved_epic
    t = _load(ws, new)
    hops.approve_together(new, requirements_hash=gate_hash(t, "requirements"), plan_hash=gate_hash(t, "plan"))
    stamps = _stamps(ws, new)
    aops.set_section(eid, "Out of scope", "anything mobile")
    hops.approve(eid, "requirements")
    assert _stamps(ws, new) == stamps  # already approved for that hash: no second gate.approved
    assert ledger.gate_verification(ws, _load(ws, new), "requirements") == "verified"


def test_an_agent_cannot_approve_a_child_this_way(ws, aops, approved_epic):
    eid, old, new = approved_epic
    t = _load(ws, new)
    with pytest.raises(HumanOnlyError):
        aops.approve(new, "requirements", expected_hash=gate_hash(t, "requirements"))
    with pytest.raises(HumanOnlyError):
        aops.approve_together(new, requirements_hash=gate_hash(t, "requirements"), plan_hash=gate_hash(t, "plan"))
    t = _load(ws, new)
    assert t.status == "backlog" and gate_state(t, "requirements") == "pending"
    assert not [e for e in ledger.entries(ws) if e.get("ticket") == new]
    from orch.errors import ValidationError
    with pytest.raises(ValidationError, match="no delegation"):
        aops.epic_auto_approve(new)  # no delegation was chosen: the agent has no other way in


def test_a_changed_child_text_is_refused(ws, hops, aops, approved_epic):
    eid, old, new = approved_epic
    seen = gate_hash(_load(ws, new), "requirements")
    aops.set_section(new, "Requirements", "r, changed while the human read it")
    from orch.errors import ValidationError
    with pytest.raises(ValidationError, match="changed since you opened it"):
        hops.approve(new, "requirements", expected_hash=seen)
    assert gate_state(_load(ws, new), "requirements") == "pending"


# -- the CLI: only that child is shown ------------------------------------------------------------------------

@pytest.fixture
def switch(monkeypatch, ws_root):
    monkeypatch.delenv("ORCH_HARNESS", raising=False)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    answer = {"v": ""}
    monkeypatch.setattr("builtins.input", lambda prompt="": answer["v"])
    return answer


def test_cli_one_step_shows_only_the_new_child(switch, ws, approved_epic, capsys):
    eid, old, new = approved_epic
    switch["v"] = new
    assert run(["approve", new]) == 0
    out = capsys.readouterr().out
    assert new in out and "Approving the requirements of" in out and "Approving the plan of" in out
    assert f"of {old}" not in out and "Billing revamp" not in out and "approve the epic" not in out
    t = _load(ws, new)
    assert t.status == "open" and gate_state(t, "plan") == "approved"


def test_cli_epic_show_points_to_the_child_only_approval(switch, ws, approved_epic, capsys):
    eid, old, new = approved_epic
    assert run(["epic", "show", eid]) == 0
    out = capsys.readouterr().out
    assert f"orch approve {new}" in out
