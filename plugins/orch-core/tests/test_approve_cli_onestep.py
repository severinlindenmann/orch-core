"""#210: `orch approve <id>` with no gate (or `all`) approves requirements and plan in one typed confirmation
(Ops.approve_together) when both wait in backlog, else the one gate that waits."""
import pytest

from orch import actor
from orch.cli import run
from orch.core import ledger, store
from orch.core.gates import gate_hash, gate_state

SECTIONS = {"Requirements": "- r1", "Acceptance criteria": "- [ ] a1", "Plan": "1. do it"}


@pytest.fixture
def switch(monkeypatch, ws_root):
    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", "s-1")
            monkeypatch.setattr(actor, "is_interactive", lambda: False)

        def human(self, confirm):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)
            monkeypatch.setattr("builtins.input", lambda prompt="": confirm)

    s = Switch()
    s.agent()
    return s


@pytest.mark.parametrize("args", [[], ["all"]])
def test_no_gate_approves_both_in_one_confirmation(switch, ws, put, capsys, args):
    tid = put("backlog", sections=SECTIONS)
    t0 = store.load(ws, tid)[1]
    rh, ph = gate_hash(t0, "requirements"), gate_hash(t0, "plan")
    switch.human(tid)
    assert run(["approve", tid, *args]) == 0
    out = capsys.readouterr().out
    assert "- r1" in out and "1. do it" in out
    assert rh.split(":")[1][:8] in out and ph.split(":")[1][:8] in out
    t = store.load(ws, tid)[1]
    assert t.status == "open"
    assert gate_state(t, "requirements") == "approved" and gate_state(t, "plan") == "approved"
    signed = {(e["gate"], e["hash"]) for e in ledger.entries(ws) if e["kind"] == "gate" and e["ticket"] == tid}
    assert signed == {("requirements", rh), ("plan", ph)}


def test_wrong_confirmation_applies_nothing(switch, ws, put):
    tid = put("backlog", sections=SECTIONS)
    switch.human("nope")
    assert run(["approve", tid]) != 0
    t = store.load(ws, tid)[1]
    assert t.status == "backlog" and gate_state(t, "requirements") == "pending"


def test_no_gate_is_human_only(switch, ws, put):
    tid = put("backlog", sections=SECTIONS)
    assert run(["approve", tid]) == 3  # agent refused
    assert run(["approve", tid, "all"]) == 3
    assert gate_state(store.load(ws, tid)[1], "requirements") == "pending"


def test_no_gate_without_a_plan_approves_the_requirements_alone(switch, ws, put):
    tid = put("backlog", sections={k: v for k, v in SECTIONS.items() if k != "Plan"})
    switch.human(tid)
    assert run(["approve", tid]) == 0
    t = store.load(ws, tid)[1]
    assert t.status == "open" and gate_state(t, "requirements") == "approved" and gate_state(t, "plan") == "pending"


def test_no_gate_approves_the_single_pending_plan(switch, ws, put, hops, aops):
    tid = put("backlog", sections=SECTIONS)
    switch.human(tid)
    hops.approve(tid, "requirements")
    aops.claim(tid)
    assert run(["approve", tid]) == 0
    assert gate_state(store.load(ws, tid)[1], "plan") == "approved"


def test_no_gate_with_nothing_pending_says_so(switch, ws, put, hops, capsys):
    tid = put("backlog", sections=SECTIONS)
    t0 = store.load(ws, tid)[1]
    switch.human(tid)
    hops.approve_together(tid, requirements_hash=gate_hash(t0, "requirements"), plan_hash=gate_hash(t0, "plan"))
    assert run(["approve", tid]) != 0
    assert "no gate" in capsys.readouterr().err


def test_dry_run_writes_nothing(switch, ws, put, capsys):
    tid = put("backlog", sections=SECTIONS)
    switch.human("")
    assert run(["approve", tid, "--dry-run"]) == 0
    assert "would approve the requirements" in capsys.readouterr().out
    assert gate_state(store.load(ws, tid)[1], "requirements") == "pending"
