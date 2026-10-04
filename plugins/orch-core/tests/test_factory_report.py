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
    assert factory_report.cards(ws) == {"ready": [], "stopped": [], "suspect": []}


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
    assert factory_report.cards(fws) == {"ready": [], "stopped": [], "suspect": []}  # a done epic is neither


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


def test_ready_does_not_hide_a_stop(fws, fa, epic, close_tasks, monkeypatch):
    from orch import clock
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))  # budget gone, work done
    cards = factory_report.cards(fws)
    assert [r["epic"] for r in cards["ready"]] == [epic.id] and [s["epic"] for s in cards["stopped"]] == [epic.id]


def _forge(fws, tid, **meta):
    path, t = store.load(fws, tid)
    t.meta.update(meta)
    store.save(fws, t, path)


def test_a_forged_testing_status_does_not_make_the_epic_ready(fws, fa, epic, close_tasks):
    c1, c2 = _child(fa, epic.id, "one"), _child(fa, epic.id, "two")
    _to_testing(fa, c1, close_tasks)
    fa.claim(c2)
    fa.set_section(c2, "Verification", PROOF)  # evidence written, but the work was never moved to testing
    _forge(fws, c2, status="testing")
    assert _ready(fws, epic.id) is None
    sus = factory_report.cards(fws)["suspect"]
    assert sus == [{"epic": epic.id, "title": "Billing revamp", "children": [c2]}]


def test_testing_needs_its_tasks_closed_and_the_claiming_session(fws, fa, epic, close_tasks, other_agent):
    from orch.core.ops import Ops
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    assert _ready(fws, epic.id)
    path, t = store.load(fws, c)
    t.set_section("Tasks", t.section("Tasks").replace("[x]", "[ ]"))  # a task reopened behind orch's back
    store.save(fws, t, path)
    assert _ready(fws, epic.id) is None


def test_a_move_into_testing_by_another_session_does_not_count(fws, fa, epic, close_tasks, other_agent):
    from orch.core.ops import Ops
    c = _child(fa, epic.id)
    fa.claim(c)
    close_tasks(fa, c)
    fa.set_section(c, "Verification", PROOF)
    Ops(fws, other_agent).move(c, "testing")
    assert _ready(fws, epic.id) is None


def test_a_forged_done_child_is_not_done_for_ready(fws, fa, epic, close_tasks):
    c1, c2 = _child(fa, epic.id, "one"), _child(fa, epic.id, "two")
    _to_testing(fa, c1, close_tasks)
    _forge(fws, c2, status="done")  # no signed verdict behind it
    assert _ready(fws, epic.id) is None
    assert factory_report.cards(fws)["suspect"][0]["children"] == [c2]


def test_a_done_child_with_a_signed_verdict_counts(fws, fa, fh, epic, close_tasks):
    c1, c2 = _child(fa, epic.id, "one"), _child(fa, epic.id, "two")
    _to_testing(fa, c1, close_tasks)
    _to_testing(fa, c2, close_tasks)
    fh.verdict(c1, "done", expected_hash=epics.verdict_hash([store.load(fws, c1)[1]], fws))
    rep = _ready(fws, epic.id)
    assert [(r["id"], r["status"]) for r in rep["children"]] == [(c1, "done"), (c2, "testing")]


def test_a_forged_ready_does_not_hide_a_real_stop(fws, fa, fh, epic, close_tasks):
    c = _child(fa, epic.id)
    fa.claim(c)
    r = permits.request(fws, fa.actor, store.load(fws, c)[1], CMD, reason="deploy")
    permits.permit_deny(fws, fh.actor, r["id"], expected_sha=r["sha"])
    fa.set_section(c, "Verification", PROOF)
    _forge(fws, c, status="testing")  # claims to be finished to hide the denial
    cards = factory_report.cards(fws)
    assert cards["ready"] == [] and [x["code"] for x in cards["stopped"][0]["reasons"]] == ["denied"]


def test_a_cut_ledger_keeps_the_other_reasons(fws, fa, fh, epic, monkeypatch):
    from orch import clock
    c = _child(fa, epic.id)
    fa.claim(c)
    r = permits.request(fws, fa.actor, store.load(fws, c)[1], CMD, reason="deploy")
    permits.permit_deny(fws, fh.actor, r["id"], expected_sha=r["sha"])
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    monkeypatch.setattr(ledger, "head_ok", lambda: False)
    assert {w["code"] for w in _stopped(fws, epic.id)} == {"ledger-cut", "budget", "denied"}


def test_the_card_shows_everything_the_verdict_binds(fws, fa, epic, close_tasks):
    c = _child(fa, epic.id)
    long = PROOF + "\n" + "\n".join(f"- AC1: more evidence line {i} with detail" for i in range(40))
    _to_testing(fa, c, close_tasks, proof=long)
    row = _ready(fws, epic.id)["children"][0]
    assert row["verification"] == long and len(long) > 400
    assert row["ac"] == "- [ ] a"


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


def test_wait_on_the_epic_wakes_once_per_state(fws, fa, epic, close_tasks, monkeypatch):
    from orch import clock
    from orch.core.events import last_seq
    from orch.core.wait import wait_for_human
    c = _child(fa, epic.id)
    assert wait_for_human(fws, epic.id, after=last_seq(fws), timeout=0.1, poll=0.01) is None
    _to_testing(fa, c, close_tasks)
    ev = wait_for_human(fws, epic.id, after=last_seq(fws), timeout=1, poll=0.01)
    assert ev.kind == "factory.ready" and ev.ticket == epic.id and ev.actor.startswith("orch:")
    cursor = ev.data["cursor"]
    # the printed cursor does not wake on the same state again
    assert wait_for_human(fws, epic.id, after=cursor, timeout=0.2, poll=0.01) is None
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    ev2 = wait_for_human(fws, epic.id, after=cursor, timeout=1, poll=0.01)
    assert ev2.kind == "factory.stopped"  # ready, and the budget is gone: a dead end outranks
    assert ev2.data["cursor"] != cursor  # the state changed (budget gone), so it wakes once more


def test_wait_on_a_child_ignores_the_factory_state(fws, fa, epic, close_tasks):
    from orch.core.events import last_seq
    from orch.core.wait import wait_for_human
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    assert wait_for_human(fws, c, after=last_seq(fws), timeout=0.2, poll=0.01) is None


def test_wait_looks_at_the_factory_rarely(fws, fa, epic, monkeypatch):
    from orch.core import factory_report as fr
    from orch.core.events import last_seq
    from orch.core.wait import wait_for_human
    calls = []
    monkeypatch.setattr(fr, "signal", lambda ws, e: calls.append(1))
    _child(fa, epic.id)
    wait_for_human(fws, epic.id, after=last_seq(fws), timeout=0.3, poll=0.01)
    assert len(calls) == 1  # once at the start, then only when the event log changes or FACTORY_EVERY passes


def test_wait_cli_prints_a_cursor_that_does_not_wake_again(fws, fa, epic, close_tasks, capsys):
    import json
    from orch.cli import run
    c = _child(fa, epic.id)
    _to_testing(fa, c, close_tasks)
    from orch.core.events import last_seq
    assert run(["wait", epic.id, "--after", str(last_seq(fws)), "--timeout", "2", "--json"]) == 0
    cursor = json.loads(capsys.readouterr().out)["cursor"]
    assert isinstance(cursor, str) and ":ready:" in cursor
    assert run(["wait", epic.id, "--after", cursor, "--timeout", "0.2", "--json"]) == 7
    assert run(["wait", epic.id, "--after", "not-a-cursor", "--timeout", "0.2"]) != 0


def test_wait_ignores_factory_state_for_an_ordinary_ticket(ws, aops):
    from orch.core.wait import wait_for_human
    t = aops.new("plain")
    assert wait_for_human(ws, t.id, timeout=0.1, poll=0.01) is None
