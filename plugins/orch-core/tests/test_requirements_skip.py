"""#172: `gates.requirements_skip_sizes` lets a ticket of a listed size leave the backlog without an approved
requirements gate. The move out of backlog stays the human's, and the rest of the flow is unchanged."""
import pytest

from orch.config.load import DEFAULTS, deep_merge, validate_schema
from orch.core import store
from orch.core.check import run_checks
from orch.core.events import Actor
from orch.core.gates import gate_state, requirements_required
from orch.core.lifecycle import allowed_targets, check_move
from orch.core.ops import Ops
from orch.core.rules import render_rules
from orch.dashboard.data.cards import _gates
from orch.dashboard.data.steps import steps, your_move
from orch.errors import HumanOnlyError, ValidationError
from orch.instructions.render import agents_rules

from conftest import human_ops

SKIP = {"gates": {"requirements_skip_sizes": ["xs"]}}


@pytest.fixture
def skip_ws(configure):
    return configure(**SKIP)


@pytest.fixture
def ops_pair(skip_ws, agent, human):
    return Ops(skip_ws, agent), human_ops(skip_ws, human)


def _codes(ws):
    return {f.code for f in run_checks(ws)}


def test_default_is_empty_and_schema_accepts_it():
    assert DEFAULTS["gates"]["requirements_skip_sizes"] == []
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "acme", **SKIP})) == []
    bad = validate_schema(deep_merge(DEFAULTS, {"customer": "acme", "gates": {"requirements_skip_sizes": ["huge"]}}))
    assert bad and "requirements_skip_sizes" in bad[0]


def test_requirements_required(ws, skip_ws, put):
    t = store.load(ws, put("backlog", size="xs"))[1]
    assert requirements_required(ws, t)  # default: every size needs its requirements approved
    assert not requirements_required(skip_ws, t)
    t.meta["size"] = "m"
    assert requirements_required(skip_ws, t)
    t.meta.update(size="xs", type="epic")
    assert requirements_required(skip_ws, t)  # an epic's approval is its charter: never skipped


def test_check_move_out_of_backlog(ws, put, human, agent):
    t = store.load(ws, put("backlog", size="xs"))[1]
    with pytest.raises(ValidationError, match="requirements gate is pending"):
        check_move(t, "open", human, plan_skip_sizes=("xs",))
    check_move(t, "open", human, plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))
    with pytest.raises(HumanOnlyError):  # skipping the gate does not make the move an agent's
        check_move(t, "open", agent, plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))
    t.meta["size"] = "s"
    with pytest.raises(ValidationError):
        check_move(t, "open", human, plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))
    assert "open" not in allowed_targets(t, human, plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))
    t.meta["size"] = "xs"
    assert "open" in allowed_targets(t, human, plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))
    assert "open" not in allowed_targets(t, agent, plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))


def test_default_config_still_needs_the_gate(ws, aops, hops):
    t = aops.new("Submit expenses", size="xs")
    assert aops.warnings  # empty Requirements / Acceptance criteria: the gate would refuse
    with pytest.raises(ValidationError, match="requirements gate"):
        hops.move(t.id, "open")


def test_small_ticket_runs_the_normal_flow_without_requirements(skip_ws, ops_pair, close_tasks):
    aops, hops = ops_pair
    t = aops.new("Submit expenses", size="xs")
    assert aops.warnings == []  # nothing to warn about: this size needs no Requirements
    with pytest.raises(HumanOnlyError):
        aops.move(t.id, "open")
    assert hops.move(t.id, "open").status == "open"
    assert "status-without-gate" not in _codes(skip_ws)
    assert aops.claim(t.id).status == "in-progress"
    close_tasks(aops, t.id)
    aops.set_section(t.id, "Verification", "submitted, receipt 123")
    assert aops.move(t.id, "testing").status == "testing"
    with pytest.raises(HumanOnlyError):
        aops.verdict(t.id, "done", expected_hash="sha256:x")
    done = hops.verdict(t.id, "done")
    assert done.status == "done" and gate_state(done, "requirements") == "pending"
    assert "status-without-gate" not in _codes(skip_ws)
    assert hops.reopen(t.id, "one more receipt").status == "open"  # no requirements to re-approve


def test_listed_size_can_still_be_approved(skip_ws, ops_pair):
    aops, hops = ops_pair
    t = aops.new("Book a room", size="xs")
    aops.set_section(t.id, "Requirements", "room for 6")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] booked")
    t = hops.approve(t.id, "requirements")
    assert t.status == "open" and gate_state(t, "requirements") == "approved"


def test_other_sizes_keep_the_gate_and_check_still_flags_them(skip_ws, ops_pair, put):
    aops, hops = ops_pair
    t = aops.new("Real feature", size="m")
    with pytest.raises(ValidationError, match="requirements gate"):
        hops.move(t.id, "open")
    put("open", size="m")
    assert "status-without-gate" in _codes(skip_ws)


def test_guard_freezes_size_once_a_skipped_ticket_left_the_backlog(skip_ws, ops_pair):
    """The human opened the ticket at its size without an approval: an agent may not resize it afterwards (a larger
    size would then be worked without the requirements it needs)."""
    from orch.hooks.guard import evaluate
    aops, hops = ops_pair
    t = aops.new("Send the report", size="xs")

    def edit(old, new):
        path = store.resolve(skip_ws, t.id).path
        return evaluate(skip_ws, {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": old,
                                                                       "new_string": new}})
    assert edit("size: xs", "size: s").allow  # still in backlog: the agent sizes the ticket
    hops.move(t.id, "open")
    d = edit("size: xs", "size: m")
    assert not d.allow and "size" in d.reason
    d = edit("type: feature", "type: chore")
    assert not d.allow and "type" in d.reason


def test_rules_and_instructions_mention_the_setting_only_when_set():
    off, on = deep_merge(DEFAULTS, {"customer": "acme"}), deep_merge(DEFAULTS, {"customer": "acme", **SKIP})
    assert "requirements gate skipped" not in render_rules(off)
    assert "requirements gate skipped for sizes: xs" in render_rules(on)
    assert "skip the requirements gate" not in agents_rules(off)
    assert "Tickets of size `xs` skip the requirements gate" in agents_rules(on)


def test_dashboard_shows_the_gate_as_skipped(skip_ws, ws, put):
    t = store.load(ws, put("backlog", size="xs"))[1]
    req = steps(t, plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))[0]
    assert req == {"name": "Requirements", "state": "skipped", "note": "skipped"}
    assert steps(t, plan_skip_sizes=("xs",))[0]["state"] == "current"  # not listed: the gate is the next step
    assert _gates(t, [], ("xs",), ("xs",))["requirements"]["state"] == "skipped"
    assert _gates(t, [], ("xs",))["requirements"]["state"] == "pending"
    move = your_move(t, [], plan_skip_sizes=("xs",), requirements_skip_sizes=("xs",))
    assert move["kind"] == "agent" and "skips the requirements gate" in move["text"]


def test_ticket_page_renders_a_skipped_ticket(skip_ws, put):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    tid = put("backlog", size="xs")
    client = TestClient(create_app(skip_ws, "tok"))
    assert client.get("/?token=tok").status_code == 200
    html = client.get(f"/t/{tid}").text
    assert "skipped for this size" in html
    assert '<option>open</option>' in html or 'value="open"' in html
