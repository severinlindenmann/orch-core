"""Host mode of the verify trust rule (F1 "Receipts", §0.8 "Trust of the command"): the CLI runs nothing before the host
said the caller may run the command (an approved plan's, or its own person's) and that the caller holds the claim and
the lease (a dry-run ``task.done``); the host refuses a receipt that breaks the rule."""

from __future__ import annotations

import base64

import pytest

from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.lifecycle_helpers import Lab
from tests.host.test_hostclient import hcli, hosted
from tests.ops.helpers import OTHER, Cli
from tests.ops.test_verify_trust import BOB_SESSION, second_person, touch


@pytest.fixture
def lab(tmp_path):
    import errno

    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def bob_of(lab):
    return hcli(lab, session=BOB_SESSION, ORCH_GRANT=second_person(lab.ws))


def done_events(lab, ref="1"):
    return [e for e in lab.ws.events(ref) if e["type"] == "task.done"]


def fill(agent, verify):
    for sec in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification"):
        assert agent("section", "set", sec, "-m", f"text of {sec}").code == 0, sec
    assert agent("ac", "add", "it works").code == 0
    assert agent("task", "add", "do it", "--verify", verify, "--proves", "AC1").code == 0


def approve(served, uid, *gates):
    ws = served.lab.ws

    def run(store):
        store.ticket(uid)
        for g in gates:
            ws.tick()
            ws.approve(uid, g)

    served.append(run)


def test_a_foreign_unapproved_command_never_runs_in_host_mode(lab, tmp_path):
    marker = tmp_path / "M"
    served = hosted(lab)
    try:
        bob = bob_of(lab)
        assert bob("new", "A").code == 0 and bob("claim", "1").code == 0
        assert bob("task", "add", "x", "--verify", touch(marker)).code == 0
        assert bob("release").code == 0
        c = hcli(lab)
        assert c("claim", "1").code == 0 and c("task", "start", "T1").code == 0
        r = c.j("task", "done", "T1", "--run")
        assert (r.code, r.err_code) == (3, "verify.unapproved"), r.out
        assert lab.ws.people["bob"].ref in r.doc["error"]["message"]
        assert not marker.exists() and done_events(lab) == []
    finally:
        served.close()


def test_a_session_without_the_claim_runs_nothing_in_host_mode(lab, tmp_path):
    """The probe of the finding: the host refused, but only after the CLI had run the command."""
    m1, m2 = tmp_path / "M1", tmp_path / "M2"
    served = hosted(lab)
    try:
        c = hcli(lab)
        assert c("new", "A").code == 0 and c("claim", "1").code == 0
        assert c("task", "add", "x", "--verify", touch(m1)).code == 0
        assert c("task", "add", "y", "--verify", touch(m2)).code == 0
        assert c("task", "start", "T1").code == 0
        # the same person's own command, but another session without the claim
        r = hcli(lab, session=OTHER).j("task", "done", "DEMO-0001/T1", "--run")
        assert (r.code, r.err_code) == (4, "claim.required"), r.out
        # no session at all, a task never started
        r = hcli(lab, session=None).j("task", "done", "DEMO-0001/T2", "--run")
        assert r.code != 0, r.out
        assert not m1.exists() and not m2.exists() and done_events(lab) == []
    finally:
        served.close()


def test_a_task_leased_by_another_session_runs_nothing_in_host_mode(lab, tmp_path):
    marker = tmp_path / "M"
    served = hosted(lab)
    try:
        c = hcli(lab)
        assert c("new", "A").code == 0 and c("claim", "1").code == 0
        assert c("task", "add", "x", "--verify", touch(marker)).code == 0
        sub = Cli(lab.ws, session=SESSION + ".1", ORCH_HOST_RUN_DIR=str(lab.run))
        assert sub("task", "start", "T1").code == 0  # the lease is the sub-agent's
        r = c.j("task", "done", "T1", "--run")
        assert (r.code, r.err_code) == (4, "lease.held"), r.out
        assert not marker.exists() and done_events(lab) == []
    finally:
        served.close()


def test_an_approved_plan_and_the_callers_own_command_run_in_host_mode(lab, tmp_path):
    foreign, own = tmp_path / "M1", tmp_path / "M2"
    served = hosted(lab)
    try:
        bob = bob_of(lab)
        assert bob("new", "A").code == 0 and bob("claim", "1").code == 0
        fill(bob, touch(foreign))
        assert bob("release").code == 0
        approve(served, lab.ws.uid("1"), "requirements", "plan")
        c = hcli(lab)
        assert c("claim", "1").code == 0 and c("task", "start", "T1").code == 0
        r = c.j("task", "done", "T1", "--run")
        assert r.code == 0, r.out
        assert foreign.exists()
        assert c("task", "add", "mine", "--verify", touch(own)).code == 0  # plan generation moves: own rule only
        assert c("task", "start", "T2").code == 0
        assert c.j("task", "done", "T2", "--run").code == 0
        assert own.exists() and len(done_events(lab)) == 2
    finally:
        served.close()


# --- the host re-checks the rule when it takes a receipt ------------------------------------------------------------

OUT = b"ok\n"


def receipt(cmd):
    return {"cmd": cmd, "exit": 0, "duration_ms": 1, "commit": None, "repo": None}


def send(client, grant, key, cmd, tid="T1"):
    args = {"task": f"{key}/{tid}", "receipt": receipt(cmd), "receipt_output": "-"}
    files = {"receipt_output": {"b64": base64.b64encode(OUT).decode()}}
    return client.req("task.done", args, grant=grant, session=SESSION, idem=True, files=files)


def test_the_host_refuses_a_receipt_for_a_foreign_unapproved_command(lab, tmp_path):
    from tests.host.dispatch_helpers import Served

    served = Served(lab)
    try:
        cmd = touch(tmp_path / "M")
        bob = bob_of(lab)
        assert bob("new", "A").code == 0 and bob("claim", "1").code == 0
        fill(bob, cmd)
        assert bob("release").code == 0
        c, g, key = served.client(), served.grant, "DEMO-0001"
        ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
        ok(c.req("task.start", {"task": f"{key}/T1"}, grant=g, session=SESSION, idem=True))
        assert err(send(c, g, key, cmd)) == "verify.unapproved"
        assert done_events(lab) == []
        # approved at the current generation: taken
        approve(served, lab.ws.uid("1"), "requirements", "plan")
        ok(send(c, g, key, cmd))
        assert len(done_events(lab)) == 1
    finally:
        served.close()


def test_the_host_refuses_a_receipt_after_the_plan_moved(lab, tmp_path):
    from tests.host.dispatch_helpers import Served

    served = Served(lab)
    try:
        cmd = touch(tmp_path / "M")
        bob = bob_of(lab)
        assert bob("new", "A").code == 0 and bob("claim", "1").code == 0
        fill(bob, cmd)
        assert bob("release").code == 0
        approve(served, lab.ws.uid("1"), "requirements", "plan")
        bob = hcli(lab, session=BOB_SESSION, ORCH_GRANT=bob.env_kw["ORCH_GRANT"])
        assert bob("claim", "1").code == 0 and bob("task", "add", "more", "--verify", "true").code == 0
        assert bob("release").code == 0
        c, g, key = served.client(), served.grant, "DEMO-0001"
        ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
        ok(c.req("task.start", {"task": f"{key}/T1"}, grant=g, session=SESSION, idem=True))
        assert err(send(c, g, key, cmd)) == "verify.unapproved"
        assert done_events(lab) == []
    finally:
        served.close()


def test_an_older_host_without_verify_runnable_runs_nothing(lab, tmp_path, monkeypatch):
    from orch import hostclient

    marker = tmp_path / "M"
    served = hosted(lab)
    try:
        c = hcli(lab)
        assert c("new", "A").code == 0 and c("claim", "1").code == 0
        assert c("task", "add", "x", "--verify", touch(marker)).code == 0
        assert c("task", "start", "T1").code == 0
        orig = hostclient._request

        def older(target, op, args, **kw):
            doc = orig(target, op, args, **kw)
            if op == "task.list":
                for t in doc["data"]["tasks"]:
                    t.pop("verify_runnable", None)
                    t.pop("verify_author", None)
            return doc

        monkeypatch.setattr(hostclient, "_request", older)
        r = c.j("task", "done", "T1", "--run")
        assert (r.code, r.err_code) == (3, "verify.unapproved"), r.out
        assert not marker.exists() and done_events(lab) == []
    finally:
        served.close()


def test_an_unattended_caller_gets_no_receipt_taken_for_an_unapproved_command(lab, tmp_path):
    """End to end on the host: a request without a grant (no person) under the claim's session."""
    from tests.host.dispatch_helpers import Served

    served = Served(lab)
    try:
        cmd = touch(tmp_path / "M")
        c, g, key = served.client(), served.grant, "DEMO-0001"
        ok(c.req("new", {"title": "A"}, grant=g, session=SESSION, idem=True))
        ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
        ok(c.req("task.add", {"ref": key, "text": "x", "verify": cmd}, grant=g, session=SESSION, idem=True))
        ok(c.req("task.start", {"task": f"{key}/T1"}, grant=g, session=SESSION, idem=True))
        rows = ok(c.req("task.list", {"ref": key}, session=SESSION))["data"]["tasks"]
        assert rows[0]["verify_runnable"] is False
        assert err(send(c, None, key, cmd)) == "op.not_allowed"  # no person: column N writes no task.done
        assert done_events(lab) == []
    finally:
        served.close()
