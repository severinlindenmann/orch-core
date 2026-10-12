"""``task done --run`` runs a task's verify command only if a person approved it in the plan or the caller's own person
wrote it last (F1 "Receipts"): any agent holding the claim can write ``verify``, no person signs it. Direct mode."""

from __future__ import annotations

from pathlib import Path

from orch import canon, crypto
from orch.identity import new_ulid
from orch.store.render import thaw
from tests.identity.helpers import Person
from tests.ops.helpers import OTHER, SESSION, Cli, Ws
from tests.ops.test_task_ac import py
from tests.store.helpers import GRANT_SECRET, ts

BOB_SESSION = "s_01J9ZP0000000000000000000B"


def second_person(ws: Ws, name: str = "bob") -> str:
    """A maintainer ``name`` with a grant of their own (scope all); returns its ``ORCH_GRANT`` value."""
    p = Person()
    old, ws.store = ws.store, ws.other()
    try:
        ws.store.append(
            ws.person_event(
                ws.owner,
                "workspace",
                "member.added",
                person=p.ref,
                name=name,
                role="maintainer",
                pk_pub=crypto.b64u(p.pk_pub),
                device_cert=p.cert(created=ws.clock[0] * 1000),
            ),
            log="workspace",
        )
        gid, issued = "gr_" + new_ulid(), ws.clock[0]
        ws.store.append(
            ws.person_event(
                p,
                "workspace",
                "grant.issued",
                grant=gid,
                scope="all",
                verbs="agent",
                issued_at=ts(issued),
                hours=8,
                expires_at=ts(issued + 3600 * 8),
                secret_hash=canon.grant_secret_hash(GRANT_SECRET),
                label=name,
            ),
            log="workspace",
        )
    finally:
        ws.store.close()
        ws.store = old
    ws.people[name] = p
    return f"{gid}.{crypto.b64u(GRANT_SECRET)}"


def touch(marker: Path) -> str:
    """A verify command that leaves ``marker`` behind when it runs."""
    return py(f"open({str(marker)!r}, 'w').close()")


def fill(agent, verify: str) -> None:
    """Everything the requirements and plan gates need, with task T1 running ``verify``."""
    for sec in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification"):
        assert agent("section", "set", sec, "-m", f"text of {sec}").code == 0, sec
    assert agent("ac", "add", "it works").code == 0
    assert agent("task", "add", "do it", "--verify", verify, "--proves", "AC1").code == 0


def done_events(ws, ref="1"):
    return [e for e in ws.events(ref) if e["type"] == "task.done"]


def bob_writes(ws, marker, *, filled: bool = False):
    """Bob's agent creates DEMO-0001 with task T1 running ``touch(marker)`` and lets the claim go; returns his Cli."""
    bob = Cli(ws, session=BOB_SESSION, ORCH_GRANT=second_person(ws))
    assert bob("new", "A ticket").code == 0
    assert bob("claim", "1").code == 0
    if filled:
        fill(bob, touch(marker))
    else:
        assert bob("task", "add", "do it", "--verify", touch(marker)).code == 0
    assert bob("release").code == 0, "release"
    return bob


def test_a_foreign_unapproved_command_never_runs(ws, cli, tmp_path):
    marker = tmp_path / "M"
    bob_writes(ws, marker)
    assert cli("claim", "1").code == 0
    assert cli("task", "start", "T1").code == 0
    for extra in ((), ("--dry-run",)):
        r = cli.j("task", "done", "T1", "--run", *extra)
        assert (r.code, r.err_code) == (3, "verify.unapproved"), r.out
        msg = r.doc["error"]["message"]
        assert f"for {ws.people['bob'].ref}" in msg and "approve" in msg and "your own verify" in msg
    assert not marker.exists() and done_events(ws) == []


def test_the_task_list_says_whose_command_it_is_and_whether_the_caller_may_run_it(ws, cli, tmp_path):
    bob = bob_writes(ws, tmp_path / "M")
    mine = cli.j("task", "list", "--ref", "1").data["tasks"][0]
    assert mine["verify_runnable"] is False and mine["verify_author"].endswith(f"for {ws.people['bob'].ref}")
    assert bob.j("task", "list", "--ref", "1").data["tasks"][0]["verify_runnable"] is True


def test_the_callers_own_command_runs_without_an_approval(ws, cli, tmp_path):
    marker = tmp_path / "M"
    assert cli("new", "A ticket").code == 0 and cli("claim", "1").code == 0
    assert cli("task", "add", "do it", "--verify", touch(marker)).code == 0
    # another session of the same person (a sub-agent under the claim) may run it too
    sub = Cli(ws, session=SESSION + ".1")
    assert sub("task", "done", "T1", "--run").code == 0
    assert marker.exists() and len(done_events(ws)) == 1


def test_an_unattended_session_never_runs_a_command_by_its_own_person_rule(ws, tmp_path):
    """An agent without a grant has no person: an unapproved command is never its own."""
    from orch.ops import verify_trust

    marker = tmp_path / "M"
    cli = Cli(ws)
    assert cli("new", "A ticket").code == 0 and cli("claim", "1").code == 0
    assert cli("task", "add", "do it", "--verify", touch(marker)).code == 0
    view = ws.view("1")
    assert verify_trust.trust(view, "T1", None) is None
    assert verify_trust.trust(view, "T1", ws.owner.ref) == "own"


def test_an_approved_plan_runs_a_foreign_command(ws, cli, tmp_path):
    marker = tmp_path / "M"
    bob_writes(ws, marker, filled=True)
    ws.human("approve", ws.uid("1"), "requirements")
    ws.human("approve", ws.uid("1"), "plan")
    assert cli("claim", "1").code == 0
    assert cli("task", "start", "T1").code == 0
    r = cli("task", "done", "T1", "--run")
    assert r.code == 0, r.err
    assert marker.exists() and done_events(ws)[0]["receipt"]["exit"] == 0


def test_a_command_added_after_the_approval_does_not_run(ws, cli, tmp_path):
    """A ``ticket.tasks`` change raises the plan generation: the approval no longer covers any verify command."""
    first, later = tmp_path / "M1", tmp_path / "M2"
    bob = bob_writes(ws, first, filled=True)
    ws.human("approve", ws.uid("1"), "requirements")
    ws.human("approve", ws.uid("1"), "plan")
    assert bob("claim", "1").code == 0
    assert bob("task", "add", "more", "--verify", touch(later)).code == 0
    assert bob("release").code == 0
    assert cli("claim", "1").code == 0
    for tid in ("T1", "T2"):
        assert cli("task", "start", tid).code == 0
        r = cli.j("task", "done", tid, "--run")
        assert (r.code, r.err_code) == (3, "verify.unapproved"), (tid, r.out)
        assert "no person approved it" in r.doc["error"]["message"]
    assert not first.exists() and not later.exists() and done_events(ws) == []


def test_a_command_changed_by_its_author_after_the_owner_wrote_it_is_the_authors(ws, cli, tmp_path):
    """The author is whoever changed ``verify`` last, not whoever added the task."""
    from orch.ops import verify_trust

    marker = tmp_path / "M"
    assert cli("new", "A ticket").code == 0 and cli("claim", "1").code == 0
    assert cli("task", "add", "do it", "--verify", "true").code == 0
    assert ws.view("1").tasks[0].verify_by["person"] == ws.owner.ref
    assert cli("release").code == 0
    grant = second_person(ws)
    bob = Cli(ws, session=BOB_SESSION, ORCH_GRANT=grant)
    assert bob("claim", "1").code == 0
    # no CLI command rewrites a task's verify: Bob's agent edits ticket.tasks with a ticket.updated of its own
    actor = {
        "kind": "agent",
        "id": "x",
        "session": BOB_SESSION,
        "for": ws.people["bob"].ref,
        "grant": grant.partition(".")[0],
    }
    old, ws.store = ws.store, ws.other()
    try:
        uid = ws.store.uid_of("1")
        ws.store.ticket(uid)
        tasks = [
            {**dict(t), "verify": {"cmd": touch(marker)}} for t in thaw(ws.store.state.tickets[uid].fields["tasks"])
        ]
        ws.tick()
        ws.store.append(
            {
                "type": "ticket.updated",
                "actor": actor,
                "set": {"ticket.tasks": tasks},
                "base_rev": ws.base_rev(uid, {"ticket.tasks": None}, {}),
            },
            log=uid,
        )
    finally:
        ws.store.close()
        ws.store = old
    view = ws.view("1")
    assert view.tasks[0].verify_by["person"] == ws.people["bob"].ref
    assert verify_trust.trust(view, "T1", ws.owner.ref) is None


def test_a_session_without_the_claim_runs_nothing(ws, cli, tmp_path):
    marker = tmp_path / "M"
    assert cli("new", "A ticket").code == 0 and cli("claim", "1").code == 0
    assert cli("task", "add", "do it", "--verify", touch(marker)).code == 0
    other = Cli(ws, session=OTHER)
    r = other.j("task", "done", "1/T1", "--run")
    assert (r.code, r.err_code) == (4, "claim.required"), r.out
    assert not marker.exists() and done_events(ws) == []
