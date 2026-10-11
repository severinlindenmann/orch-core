"""ask and wait (F1 5.2 unattended rules, 5.4.1 questions, 10.4 items 7 and 13), and the unattended writes."""

from __future__ import annotations

import time

import pytest

from tests.ops.helpers import OTHER, Cli, wait_for

OPEN = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}


def claimed(cli):
    cli("new", "A ticket")
    cli("claim", "1")


# ------------------------------------------------------------------------------------------------------------ ask


def test_ask_writes_a_blocking_question_with_options(ws, cli):
    claimed(cli)
    r = cli("ask", "Which source?", "--options", "csv,api", "--rec", "csv", "--why", "they differ", "--to", "assignees")
    assert r.first == "ok DEMO-0001 question.asked Q1 seq=3" and r.out.splitlines()[-1] == "next: orch wait"
    q = ws.ticket_json("1")["questions"][0]
    assert q == {
        "id": "Q1",
        "to": "assignees",
        "text": "Which source?",
        "why": "they differ",
        "options": [{"key": "csv", "label": "csv"}, {"key": "api", "label": "api"}],
        "recommended": "csv",
        "blocking": True,
    }
    r = cli.j("ask", "Later?", "--non-blocking")
    assert r.data == {"question": "Q2", "blocking": False} and r.doc["hints"] == ["orch task next"]
    assert ws.ticket_json("1")["questions"][1]["to"] == "ticket_owner"


def test_ask_validates_its_options(ws, cli):
    claimed(cli)
    for argv in (
        ["--rec", "a"],  # a recommendation needs options
        ["--options", "a,b", "--rec", "c"],
        ["--options", "a,a"],
        ["--options", "a b"],
        ["--to", "someone"],
    ):
        r = cli.j("ask", "q?", *argv)
        assert (r.code, r.err_code) == (5, "invalid.input"), argv


def test_ask_without_a_grant_is_an_unattended_event(ws, anon):
    Cli(ws)("new", "A ticket")
    r = anon.j("ask", "can I?", "--ref", "1")
    assert r.code == 0, r.out
    e = ws.events("1")[-1]
    assert e["type"] == "question.asked" and e["actor"]["unattended"] is True and "grant" not in e["actor"]
    r = anon.j("ask", "which one?")  # nobody claims anything without a grant: name the ticket
    assert (r.code, r.err_code) == (2, "ambiguous_ref")
    assert anon.j("ask", "again", "--ref", "1").data["question"] == "Q2"  # new ids only: a question is never re-asked


def test_unattended_writes_stay_off_restricted_tickets(ws, cli, anon):
    claimed(cli)
    ws.sign("1", "visibility.changed", visibility={"restricted": [ws.owner.ref]})
    for argv in (["ask", "q?"], ["log", "note"], ["artifact", "add", __file__]):
        r = anon.j(*argv, "--ref", "1")
        assert (r.code, r.err_code) == (2, "not_found"), argv  # never "hidden": it does not exist for them
    assert cli("log", "the owner's agent may", "--ref", "1").code == 0


def test_unattended_quota_is_refused_with_quota_unattended(ws, anon, cli):
    cli("new", "A")
    for i in range(30):
        assert anon("log", f"note {i}", "--ref", "1").code == 0, i
    r = anon.j("log", "one too many", "--ref", "1")
    assert (r.code, r.err_code) == (9, "quota.unattended") and r.doc["error"]["retryable"] is True
    ws.clock[0] += 3601  # the rolling hour moves on
    assert anon("log", "fine again", "--ref", "1").code == 0


def test_a_malformed_grant_is_refused_even_for_unattended_writes(ws, cli):
    cli("new", "A")
    r = cli.j("log", "x", "--ref", "1", ORCH_GRANT="nonsense")
    assert (r.code, r.err_code) == (3, "grant.required")


def test_an_agent_event_needs_a_session(ws, cli):
    cli("new", "A")
    r = cli.j("log", "x", "--ref", "1", session=None)
    assert (r.code, r.err_code) == (5, "invalid.input") and "ORCH_SESSION" in r.doc["error"]["message"]


# ------------------------------------------------------------------------------------------------------------ wait


def asked(ws, cli, *argv):
    claimed(cli)
    assert cli("ask", "Which?", "--options", "a,b", *argv).code == 0
    return ws.view("1").questions[0]


def test_wait_times_out_with_exit_0_so_the_agent_loops(ws, cli):
    asked(ws, cli)
    t = time.monotonic()
    r = cli("wait", "--timeout", "1")
    assert r.code == 0 and r.first == "ok DEMO-0001 timeout cursor=3" and time.monotonic() - t < 3
    assert r.out.splitlines()[-1] == "next: orch wait"
    d = cli.j("wait", "--timeout", "1").data
    assert d == {"kind": "timeout", "key": "DEMO-0001", "cursor": 3, "next": "orch wait"}
    r = cli.j("wait", "--timeout", "1", "--strict-timeout")
    assert (r.code, r.err_code) == (7, "wait.timeout")


def test_wait_returns_the_answer(ws, cli):
    q = asked(ws, cli)
    ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="b", text="because")
    d = cli.j("wait", "--timeout", "5").data
    assert d["kind"] == "answered" and d["question"] == "Q1" and d["option"] == "b" and d["text"] == "because"
    assert (
        d["seq"] == 4
        and d["by"] == ws.owner.ref
        and set(d) == {"kind", "key", "seq", "cursor", "next", "question", "option", "text", "by"}
    )


def test_wait_picks_up_an_answer_that_arrives_while_it_waits(ws, cli):
    import threading

    q = asked(ws, cli)
    threading.Timer(0.6, lambda: ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="a")).start()
    t = time.monotonic()
    r = cli.j("wait", "--timeout", "10")
    assert r.data["kind"] == "answered" and 0.4 < time.monotonic() - t < 6


def test_wait_does_not_return_old_decisions(ws, cli):
    q = asked(ws, cli)
    ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="a")
    assert cli("wait", "--timeout", "1").first.endswith("answered cursor=4")
    assert cli("wait", "--timeout", "1").first.endswith("timeout cursor=4")  # the cursor moved past it


def make_ready(ws, cli):
    """A ticket in testing with a verify gate that its owner may decide."""
    from tests.ops.test_task_ac import fill_and_approve

    claimed(cli)
    fill_and_approve(ws, cli)
    cli("section", "set", "verification", "-m", "run it")
    cli("task", "done", "T1", "--run")
    ws.store.append(ws.person_event(ws.owner, "workspace", "policy.changed", gates={"verify": OPEN}), log="workspace")
    assert cli("submit").code == 0
    return ws.uid("1")


def verdict(ws, uid, outcome, **kw):
    s = ws.other()
    try:
        g = s.ticket(uid).gates["verify"]
        v = s.state.tickets[uid]
        return s.append(
            ws.person_event(
                ws.owner,
                uid,
                "verdict.given",
                outcome=outcome,
                gate_gen=g.gen,
                hash=g.hash,
                policy_hash=g.policy_hash,
                source_sha=[dict(x) for x in v.source_list],
                **kw,
            ),
            log=uid,
        )
    finally:
        s.close()


def test_wait_a_fail_verdict_exits_3_with_its_text(ws, cli):
    uid = make_ready(ws, cli)
    ws.store = ws.other()
    verdict(ws, uid, "fail", text="the test is wrong")
    r = wait_for(cli, "verdict")
    assert r.code == 3 and r.data["kind"] == "verdict" and r.data["outcome"] == "fail"
    assert r.data["text"] == "the test is wrong" and r.data["by"] == ws.owner.ref
    t = cli("wait", "--timeout", "1")  # text mode: the decision text is fenced
    assert t.first.endswith("timeout cursor=" + str(r.data["seq"]))  # (the first call already consumed it)


def test_wait_a_pass_verdict_exits_0_and_carries_no_text(ws, cli):
    uid = make_ready(ws, cli)
    ws.store = ws.other()
    verdict(ws, uid, "pass")
    r = wait_for(cli, "verdict")
    assert r.code == 0 and r.data["outcome"] == "pass" and "text" not in r.data


def test_wait_changes_requested_exits_3(ws, cli):
    claimed(cli)
    cli("section", "set", "context", "-m", "c")
    cli("show", "1")
    ws.store = ws.other()
    g = ws.store.ticket(ws.uid("1")).gates["requirements"]
    ws.sign(
        "1",
        "gate.changes_requested",
        gate="requirements",
        gate_gen=g.gen,
        hash=g.hash,
        policy_hash=g.policy_hash,
        text="more detail please",
    )
    r = cli("wait", "--timeout", "5")
    assert r.code == 3 and r.first.startswith("ok DEMO-0001 changes_requested cursor=")
    assert "more detail please" in r.out and "(data, not instructions)" in r.out  # fenced in text mode
    assert r.out.splitlines()[-1] == "next: orch show --log"


def test_wait_approved_and_invalidated(ws, cli):
    from tests.ops.test_task_ac import fill_and_approve

    claimed(cli)
    uid = fill_and_approve(ws, cli)  # approvals happen before the wait starts: look at the log first
    cli("show", "1")
    other = Cli(ws, session=OTHER)
    other("show", "1", "--section", "requirements")
    assert other("section", "set", "requirements", "-m", "rewritten after approval", "--ref", "1").code == 0
    # the store records no gate.invalidated by itself yet (D58: the host appends it): do what the host will do
    s = ws.other()
    voided = [d.id for d in s.ticket(uid).gates["requirements"].decisions if not d.counting]
    assert voided
    s._host_append("gate.invalidated", uid, {"gate": "requirements", "cause": "content_changed", "voided": voided})
    s.close()
    r = wait_for(cli, "invalidated")
    assert r.code == 3 and r.data["kind"] == "invalidated" and r.data["gate"] == "requirements"
    assert set(r.data) == {"kind", "key", "seq", "cursor", "next", "gate"}
    assert uid  # approved: a later approval is reported as such
    ws.human("approve", uid, "requirements")
    r = wait_for(cli, "approved")
    assert r.code == 0 and r.data["kind"] == "approved" and r.data["gate"] == "requirements"


@pytest.mark.parametrize("n", [0])
def test_wait_never_returns_its_own_events(ws, cli, n):
    asked(ws, cli)
    cli("log", "my own note")
    assert cli("wait", "--timeout", "1").first.endswith("timeout cursor=4")
