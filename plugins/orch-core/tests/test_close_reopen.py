import pytest

from orch.core import store
from orch.core.events import read_events
from orch.core.gates import record_approval
from orch.errors import HumanOnlyError, TransitionError, UsageError


def test_close_is_human_only_and_logged(ws, put, hops, aops):
    tid = put("in-progress", claim={"session": "s1", "harness": "claude", "at": "2026-10-02T09:00Z"})
    with pytest.raises(HumanOnlyError):
        aops.close(tid, "GH-13 is closed in GitHub")
    t = hops.close(tid, "GH-13 is closed in GitHub")
    assert t.status == "done" and t.meta["claim"]["session"] is None
    assert "closed: GH-13 is closed in GitHub" in t.section("Log")
    e = read_events(ws)[-1]
    assert e.kind == "ticket.moved" and e.data == {"reason": "GH-13 is closed in GitHub", "command": "close",
                                                "from": "in-progress", "to": "done"}


def test_close_needs_a_reason_and_an_open_ticket(ws, put, hops):
    with pytest.raises(UsageError):
        hops.close(put("open"), "  ")
    with pytest.raises(TransitionError):
        hops.close(put("done"), "again")


def _done_with_requirements(ws, put, human):
    tid = put("done", sections={"Requirements": "Do the thing."})
    path, t = store.load(ws, tid)
    record_approval(ws, t, "requirements", human)
    t.meta["gates"]["verify"] = {"verdict": "done", "at": "2026-10-01T09:00Z", "via": "tty"}
    store.save(ws, t, path)
    return tid


def test_reopen_goes_to_open_when_requirements_are_approved(ws, put, hops, human):
    tid = _done_with_requirements(ws, put, human)
    t = hops.reopen(tid, "GH-5 is open again in GitHub")
    assert t.status == "open" and "verify" not in t.meta["gates"]
    assert read_events(ws)[-1].data["reason"] == "GH-5 is open again in GitHub"
    assert read_events(ws)[-1].data["command"] == "reopen"


def test_reopen_goes_to_backlog_otherwise(ws, put, hops, aops):
    tid = put("done")
    with pytest.raises(HumanOnlyError):
        aops.reopen(tid, "x")
    assert hops.reopen(tid, "GH-5 is open again in GitHub").status == "backlog"
    with pytest.raises(TransitionError):
        hops.reopen(put("open"), "x")


def test_timeline_says_closed_and_reopened(ws, put, hops):
    from orch.dashboard.data.timeline import describe
    hops.close(put("open"), "GH-13 is closed in GitHub")
    assert describe(read_events(ws)[-1]) == "closed the ticket (open → done): GH-13 is closed in GitHub"
    hops.reopen(put("done"), "GH-5 is open again")
    assert describe(read_events(ws)[-1]) == "reopened the ticket (done → backlog): GH-5 is open again"


def test_timeline_reads_the_command_not_the_reason(ws, put, hops):
    from orch.core.events import Event
    from orch.dashboard.data.timeline import describe
    plain = Event(1, "2026-10-02T09:00Z", "DEMO-0001", "ticket.moved", "human:you", "tty",
                  {"reason": "a note", "from": "open", "to": "done"})
    assert describe(plain) == "moved open → done"
    reopened = Event(2, "2026-10-02T09:00Z", "DEMO-0001", "ticket.moved", "human:you", "tty",
                     {"reason": "x", "command": "reopen", "from": "done", "to": "done"})
    assert describe(reopened) == "reopened the ticket (done → done): x"


def _ticket_findings(ws, tid):
    from orch.core.check import run_checks
    return [f for f in run_checks(ws, emit_events=False) if f.ticket == tid]


def test_close_skips_open_tasks_with_the_reason(ws, working, aops, hops, plan_approved):
    from orch.core import tasks as tk
    aops.task_add(working, ["Write the job", "Compare cost", "Ask about limits"])
    plan_approved(working)
    aops.task_start(working, "T1")
    aops.task_block(working, "T3", "waiting for the quota")
    t = hops.close(working, "GH-13 is closed in GitHub")
    assert [(x.id, x.state, x.why) for x in tk.ticket_tasks(t)] == [
        ("T1", "skipped", "GH-13 is closed in GitHub"), ("T2", "skipped", "GH-13 is closed in GitHub"),
        ("T3", "skipped", "GH-13 is closed in GitHub")]
    e = read_events(ws)[-1]
    assert e.data["command"] == "close" and e.data["tasks_skipped"] == ["T1", "T2", "T3"]
    assert _ticket_findings(ws, working) == []


@pytest.mark.parametrize("status, size", [("backlog", "m"), ("open", "xs"), ("in-progress", "m")])
def test_a_closed_ticket_passes_orch_check(ws, put, hops, status, size):
    tid = put(status, size=size, sections={"Tasks": "- [ ] T1 Write the job\n- [/] T2 Test it\n"})
    hops.close(tid, "GH-13 is closed in GitHub")
    assert _ticket_findings(ws, tid) == []


def test_reopen_after_close_still_works(ws, working, aops, hops):
    aops.task_add(working, ["Write the job"])
    hops.close(working, "GH-13 is closed in GitHub")
    t = hops.reopen(working, "GH-13 is open again")
    assert t.status == "open" and read_events(ws)[-1].data["command"] == "reopen"


def test_a_plain_human_move_to_done_is_still_unverified(ws, put, hops):
    from orch.core.events import Actor, append_event
    tid = put("done")
    append_event(ws, tid, "ticket.moved", Actor("human", "you", "tty"), {"reason": "x", "from": "open", "to": "done"})
    assert "unverified-verdict" in {f.code for f in _ticket_findings(ws, tid)}
