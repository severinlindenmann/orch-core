"""#208: changed (invalidated) requirements are re-approved where the ticket stands (open, in progress, waiting,
testing): same human-only, hash-bound, signed approval as in backlog; the status and the plan approval stay."""
import pytest

from orch import actor
from orch.cli import run
from orch.core import ledger, store
from orch.core.events import read_events
from orch.core.gates import gate_hash, gate_state
from orch.errors import HumanOnlyError, TransitionError, ValidationError

SECTIONS = {"Requirements": "- r1", "Acceptance criteria": "- [ ] a1", "Plan": "1. do it"}


def _approved_then_edited(ws, put, status, *, edit=True):
    tid = put(status, sections=SECTIONS)
    t = store.load(ws, tid)[1]
    for gate in ("requirements", "plan"):
        t.meta.setdefault("gates", {})[gate] = {"approved": "2026-09-30T09:00Z", "via": "dashboard",
                                                "hash": gate_hash(t, gate), "hash_v": 3}
    store.save(ws, t)
    if edit:
        t = store.load(ws, tid)[1]
        t.set_section("Requirements", "- r1\n- r2 decided in chat")
        store.save(ws, t)
    return tid


@pytest.mark.parametrize("status", ["open", "in-progress", "waiting", "testing"])
def test_changed_requirements_are_reapproved_in_place(ws, put, hops, status):
    tid = _approved_then_edited(ws, put, status)
    assert gate_state(store.load(ws, tid)[1], "requirements") == "invalidated"
    plan_before = store.load(ws, tid)[1].meta["gates"]["plan"]
    t = hops.approve(tid, "requirements")
    assert t.status == status  # the ticket stays where it is
    assert gate_state(t, "requirements") == "approved"
    assert t.meta["gates"]["plan"] == plan_before and gate_state(t, "plan") == "approved"  # the plan is untouched
    seen = gate_hash(t, "requirements")
    assert any(e["kind"] == "gate" and e["ticket"] == tid and e["gate"] == "requirements" and e["hash"] == seen
               for e in ledger.entries(ws))
    ev = [e for e in read_events(ws, tid) if e.kind == "gate.approved"][-1]
    assert ev.data["gate"] == "requirements" and ev.data["hash"] == seen and "to" not in ev.data


def test_only_an_invalidated_gate_is_reapproved_outside_backlog(ws, put, hops):
    tid = _approved_then_edited(ws, put, "in-progress", edit=False)
    assert gate_state(store.load(ws, tid)[1], "requirements") == "approved"
    with pytest.raises(TransitionError, match="approved in backlog"):
        hops.approve(tid, "requirements")


def test_a_pending_gate_outside_backlog_is_still_refused(ws, put, hops):
    tid = put("open", sections=SECTIONS)
    with pytest.raises(TransitionError, match="approved in backlog"):
        hops.approve(tid, "requirements")


def test_done_tickets_are_not_reapproved(ws, put, hops):
    tid = _approved_then_edited(ws, put, "done")
    with pytest.raises(TransitionError):
        hops.approve(tid, "requirements")


def test_an_agent_cannot_reapprove(ws, put, aops):
    tid = _approved_then_edited(ws, put, "in-progress")
    with pytest.raises(HumanOnlyError):
        aops.approve(tid, "requirements", expected_hash=gate_hash(store.load(ws, tid)[1], "requirements"))
    assert gate_state(store.load(ws, tid)[1], "requirements") == "invalidated"


def test_reapproval_is_bound_to_the_hash_shown(ws, put, hops):
    tid = _approved_then_edited(ws, put, "in-progress")
    seen = gate_hash(store.load(ws, tid)[1], "requirements")
    t = store.load(ws, tid)[1]
    t.set_section("Requirements", "- r1\n- something else an agent added meanwhile")
    store.save(ws, t)
    with pytest.raises(ValidationError, match="changed since you opened it"):
        hops.approve(tid, "requirements", expected_hash=seen)
    assert gate_state(store.load(ws, tid)[1], "requirements") == "invalidated"


def test_reapproval_refuses_open_question_lines_like_any_approval(ws, put, hops):
    tid = _approved_then_edited(ws, put, "in-progress")
    t = store.load(ws, tid)[1]
    t.set_section("Requirements", "- r1\nOpen question: which region?")
    store.save(ws, t)
    with pytest.raises(ValidationError, match="open question"):
        hops.approve(tid, "requirements")


def test_the_agent_may_work_again_after_the_reapproval(ws, put, hops):
    tid = _approved_then_edited(ws, put, "in-progress")
    t = store.load(ws, tid)[1]
    with pytest.raises(ValidationError, match="changed since it was approved") as e:
        ledger.require_signed(ws, t, ("requirements",))
    assert f"orch approve {tid} requirements" in (e.value.hint or "")
    hops.approve(tid, "requirements")
    assert gate_state(store.load(ws, tid)[1], "requirements") == "approved"


# -- the CLI --------------------------------------------------------------------------------------------------

@pytest.fixture
def cli_human(monkeypatch, ws_root):
    monkeypatch.delenv("ORCH_HARNESS", raising=False)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": TID[0])


TID = [""]


def test_cli_shows_the_diff_then_the_full_text_and_keeps_the_status(cli_human, ws, put, capsys):
    tid = _approved_then_edited(ws, put, "in-progress")
    # the approved snapshot the diff is read against (a real approval keeps it)
    from orch.core.gates import snapshot_path
    base = store.load(ws, tid)[1]
    base.set_section("Requirements", "- r1")
    snapshot_path(ws, tid, "requirements").parent.mkdir(parents=True, exist_ok=True)
    from orch.core.gates import normalized_text
    snapshot_path(ws, tid, "requirements").write_text(normalized_text(base, "requirements") + "\n", encoding="utf-8")
    TID[0] = tid
    assert run(["approve", tid, "requirements"]) == 0
    out = capsys.readouterr().out
    assert "Changed since you approved the requirements" in out
    assert "+- r2 decided in chat" in out and "The full text now:" in out
    t = store.load(ws, tid)[1]
    assert t.status == "in-progress" and gate_state(t, "requirements") == "approved"
    assert gate_state(t, "plan") == "approved"


def test_cli_without_a_gate_reapproves_the_changed_requirements(cli_human, ws, put):
    tid = _approved_then_edited(ws, put, "open")
    TID[0] = tid
    assert run(["approve", tid]) == 0
    t = store.load(ws, tid)[1]
    assert t.status == "open" and gate_state(t, "requirements") == "approved"
