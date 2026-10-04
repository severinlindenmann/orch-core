"""AI Factory phase 3 (#2): the Ready report and the Stopped message. Derived and read-only; the one human action is
the epic verdict that already exists."""
from datetime import timedelta

import pytest

from orch.core import epics, factory_report, ledger, permits, query, store
from orch.core.events import Actor, append_event
from orch.errors import HumanOnlyError

CMD = "make deploy"
PROOF = "- AC1: ran the full suite, green"


def _refine(ops, tid, plan="1. do it"):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)
    return tid


@pytest.fixture
def fws(configure):
    return configure(factory={"enabled": True})


@pytest.fixture
def fa(fws, agent):
    from orch.core.ops import Ops
    return Ops(fws, agent)


@pytest.fixture
def fh(fws, human):
    from conftest import human_ops
    return human_ops(fws, human)


@pytest.fixture
def epic(fws, fa, fh):
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True})
    return store.load(fws, e.id)[1]


def _child(fa, eid, title="child"):
    c = fa.new(title, epic=eid)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    return c.id


def _to_testing(fa, cid, close_tasks, proof=PROOF):
    fa.claim(cid)
    close_tasks(fa, cid)
    fa.set_section(cid, "Verification", proof)
    fa.move(cid, "testing")


def _ready(fws, eid):
    return factory_report.ready(fws, store.load(fws, eid)[1])


def _stopped(fws, eid):
    return factory_report.stopped(fws, store.load(fws, eid)[1])


def test_not_ready_until_every_child_is_in_testing(fws, fa, epic, close_tasks):
    c1, c2 = _child(fa, epic.id, "one"), _child(fa, epic.id, "two")
    assert _ready(fws, epic.id) is None
    _to_testing(fa, c1, close_tasks)
    assert _ready(fws, epic.id) is None  # c2 is still open
    _to_testing(fa, c2, close_tasks)
    rep = _ready(fws, epic.id)
    assert [r["id"] for r in rep["children"]] == [c1, c2]
    assert rep["proven"] == rep["total"] == 2 and rep["open"] == 2


def test_not_ready_while_a_criterion_has_no_evidence(fws, fa, epic, close_tasks):
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks, proof="nothing numbered")
    assert _ready(fws, epic.id) is None


def test_report_says_what_each_child_holds(fws, fa, epic, close_tasks):
    c = _child(fa, epic.id)
    fa.set_section(c, "Findings", "left out: <b>pdf</b> export")
    _to_testing(fa, c, close_tasks)
    row = _ready(fws, epic.id)["children"][0]
    assert row["verification"] == PROOF and row["findings"] == "left out: <b>pdf</b> export"
    assert row["how"] == "auto-approved under delegation" and row["status"] == "testing"


def test_ready_is_not_reported_for_an_ordinary_epic_or_with_the_factory_off(ws, aops, hops, close_tasks, fws, fa, fh):
    e = aops.new("Ordinary", type="epic")
    _refine(aops, e.id, plan=None)
    hops.approve(e.id, "requirements", delegate={})
    c = _child(aops, e.id)
    _to_testing(aops, c, close_tasks)
    assert factory_report.ready(ws, store.load(ws, e.id)[1]) is None
    assert factory_report.cards(ws) == {"ready": [], "stopped": []}


def test_ready_carries_the_hash_the_epic_verdict_checks_and_only_the_human_signs_it(fws, fa, fh, epic, close_tasks):
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    rep = _ready(fws, epic.id)
    tickets = [store.load(fws, c)[1]]
    assert rep["seen"] == epics.verdict_hash(tickets, fws)
    with pytest.raises(HumanOnlyError):
        fa.verdict(epic.id, "done", expected_hash=rep["seen"])
    assert store.load(fws, epic.id)[1].status == "open"
    fh.verdict(epic.id, "done", expected_hash=rep["seen"])
    assert store.load(fws, epic.id)[1].status == "done" and store.load(fws, c)[1].status == "done"
    assert factory_report.cards(fws) == {"ready": [], "stopped": []}  # a done epic is neither


def test_a_child_changed_after_the_report_is_refused_by_the_verdict(fws, fa, fh, epic, close_tasks):
    from orch.errors import ValidationError
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    seen = _ready(fws, epic.id)["seen"]
    fa.set_section(c, "Verification", "- AC1: ran the suite again")
    with pytest.raises(ValidationError):
        fh.verdict(epic.id, "done", expected_hash=seen)


# -- stopped -------------------------------------------------------------------------------------------------------

def test_a_running_factory_is_neither_ready_nor_stopped(fws, fa, epic):
    _child(fa, epic.id)
    assert _stopped(fws, epic.id) == [] and factory_report.cards(fws)["stopped"] == []


def test_stopped_when_the_time_budget_is_used_up(fws, fa, epic, monkeypatch):
    from orch import clock
    _child(fa, epic.id)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    why = _stopped(fws, epic.id)
    assert [w["code"] for w in why] == ["budget"] and "72 hours" in why[0]["text"]


def test_stopped_when_the_child_budget_is_used_up(fws, fa, fh):
    e = fa.new("Small", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True, "max_children": 1})
    _child(fa, e.id)
    assert [w["code"] for w in _stopped(fws, e.id)] == ["budget"]


def test_a_child_sent_back_three_times_stops_the_factory(fws, fa, fh, epic, close_tasks):
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    for n in range(factory_report.FAILED_TRIES):
        assert _stopped(fws, epic.id) == []
        fh.verdict(c, "follow-up", "not yet")
        if n < factory_report.FAILED_TRIES - 1:
            fa.move(c, "testing")
    why = _stopped(fws, epic.id)
    assert [w["code"] for w in why] == ["sent-back"] and c in why[0]["text"]


def test_a_denied_permission_that_still_holds_a_child_back_stops_the_factory(fws, fa, fh, epic):
    c = _child(fa, epic.id)
    fa.claim(c)
    r = permits.request(fws, fa.actor, store.load(fws, c)[1], CMD, reason="deploy")
    assert _stopped(fws, epic.id) == []  # asked, not answered: a card, not a dead end
    permits.permit_deny(fws, fh.actor, r["id"], expected_sha=r["sha"])
    why = _stopped(fws, epic.id)
    assert [w["code"] for w in why] == ["denied"] and r["id"] in why[0]["text"]


def test_a_cut_ledger_stops_the_factory(fws, fa, epic, monkeypatch):
    _child(fa, epic.id)
    monkeypatch.setattr(ledger, "head_ok", lambda: False)
    assert [w["code"] for w in _stopped(fws, epic.id)] == ["ledger-cut"]


def test_an_agent_cannot_raise_a_stop_by_writing_files_or_events(fws, fa, epic):
    c = _child(fa, epic.id)
    path, t = store.load(fws, c)
    t.meta["status"] = "waiting"
    store.save(fws, t, path)
    append_event(fws, c, "verdict.given", Actor("agent", "x", "cli"), {"verdict": "follow-up"})
    append_event(fws, c, "permit.denied", Actor("agent", "x", "cli"), {"request": "P-00000000"})
    assert _stopped(fws, epic.id) == []


def test_a_ready_epic_is_not_also_stopped(fws, fa, epic, close_tasks, monkeypatch):
    from orch import clock
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))  # budget gone, work done
    cards = factory_report.cards(fws)
    assert [r["epic"] for r in cards["ready"]] == [epic.id] and cards["stopped"] == []


# -- the human's counts and orch wait ---------------------------------------------------------------------------------

def test_waiting_and_counts_include_ready_and_stopped(fws, fa, fh, epic, close_tasks):
    before = query.counts(query.waiting(fws))["blocking"]
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    items = [i for i in query.waiting(fws) if i["kind"].startswith("factory-")]
    assert [(i["ticket"], i["kind"]) for i in items] == [(epic.id, "factory-ready")]
    # the child's own verdict is a blocking item too; the report adds one
    assert query.counts(query.waiting(fws))["blocking"] == before + 2
    fh.verdict(epic.id, "done", expected_hash=_ready(fws, epic.id)["seen"])
    assert not [i for i in query.waiting(fws) if i["kind"].startswith("factory-")]


def test_session_start_names_the_cards(fws, fa, epic, close_tasks):
    from orch.hooks.session_start import session_start_text
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    assert f"{epic.id}: the factory is ready for your verdict" in session_start_text(fws, "s")


def test_wait_wakes_on_ready_and_on_stopped(fws, fa, epic, close_tasks, monkeypatch):
    from orch import clock
    from orch.core.wait import wait_for_human
    c = _child(fa, epic.id)
    from orch.core.events import last_seq
    assert wait_for_human(fws, epic.id, after=last_seq(fws), timeout=0.1, poll=0.01) is None
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    ev = wait_for_human(fws, c, after=last_seq(fws), timeout=1, poll=0.01)  # a child's agent waits too
    assert ev.kind == "factory.stopped" and ev.ticket == epic.id and ev.actor.startswith("orch:")
    monkeypatch.setattr(clock, "now", lambda: real)
    _to_testing(fa, c, close_tasks)
    assert wait_for_human(fws, epic.id, after=last_seq(fws), timeout=1, poll=0.01).kind == "factory.ready"


def test_wait_ignores_factory_state_for_an_ordinary_ticket(ws, aops):
    from orch.core.wait import wait_for_human
    t = aops.new("plain")
    assert wait_for_human(ws, t.id, timeout=0.1, poll=0.01) is None
