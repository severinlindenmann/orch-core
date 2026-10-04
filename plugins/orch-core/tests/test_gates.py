from orch.core.gates import approved_snapshot, clear_gate, gate_hash, gate_state, plan_required, record_approval
from orch.core.model import new_ticket


def _t():
    t = new_ticket("L-0001", "x", type="feature", priority="normal", size="m", created="2026-09-30T08:00Z")
    t.set_section("Requirements", "Must back up.")
    t.set_section("Acceptance criteria", "- [ ] nightly job\n- [ ] alert on failure")
    t.set_section("Plan", "1. do it")
    return t


def test_pending_then_approved(ws, human):
    t = _t()
    assert gate_state(t, "requirements") == "pending"
    record_approval(ws, t, "requirements", human)
    g = t.meta["gates"]["requirements"]
    assert gate_state(t, "requirements") == "approved"
    assert g["via"] == "tty" and g["hash"].startswith("sha256:")
    assert "Must back up." in approved_snapshot(ws, "L-0001", "requirements")


def test_checkbox_toggle_keeps_approval(ws, human):
    t = _t()
    record_approval(ws, t, "requirements", human)
    t.set_section("Acceptance criteria", "- [x] nightly job\n- [X] alert on failure  ")
    assert gate_state(t, "requirements") == "approved"


def test_text_change_invalidates(ws, human):
    t = _t()
    record_approval(ws, t, "requirements", human)
    t.set_section("Out of scope", "Restores")
    assert gate_state(t, "requirements") == "invalidated"
    assert gate_state(t, "plan") == "pending"


def test_hash_ignores_whitespace_noise():
    a, b = _t(), _t()
    a.set_section("Requirements", "Must back up.\n\nSecond")
    b.set_section("Requirements", "Must back up.   \n\n\n\nSecond")
    assert gate_hash(a, "requirements") == gate_hash(b, "requirements")


def test_clear_gate(ws, human):
    t = _t()
    record_approval(ws, t, "plan", human)
    clear_gate(t, "plan")
    assert gate_state(t, "plan") == "pending"


def test_plan_required(ws):
    t = _t()
    assert plan_required(ws, t)
    t.meta["size"] = "xs"
    assert not plan_required(ws, t)
