"""H3b2: ``task.done`` at contract 2 (§0.8): the host never runs ``verify.cmd`` (``run:true`` is refused), a client
receipt is checked against the ticket's ``verify.cmd``, its output goes through ``files`` and is stored as a receipt
artifact, and the stored receipt is labelled ``attested_by: "agent"`` (never host-verified). Visibility as for every
ticket write and read."""

from __future__ import annotations

import base64
import hashlib
import json

from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, Served, err, ok
from tests.host.test_dispatch_matrix import as_agent_class

lab, served = human_helpers.lab, human_helpers.served

CMD = "make test"
OUT = b"collected 3 items\n3 passed\n"


def b64(data: bytes) -> dict:
    return {"b64": base64.b64encode(data).decode()}


def receipt(**kw) -> dict:
    return {"cmd": CMD, "exit": 0, "duration_ms": 1234, "commit": None, "repo": None, **kw}


def started(served: Served, verify: str | None = CMD):
    """A claimed ticket with task T1 started by the agent's session; (client, key, uid). The plan gate is off in this
    workspace (r10-P02: an agent builds only after the plan is approved; these tests are about receipts)."""
    served.lab.ws.plan_gate_off()
    uid, key = served.ticket("receipts")
    c, g = served.client(), served.grant
    ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
    add = {"ref": key, "text": "do it", **({"verify": verify} if verify else {})}
    ok(c.req("task.add", add, grant=g, session=SESSION, idem=True))
    ok(c.req("task.start", {"task": f"{key}/T1"}, grant=g, session=SESSION, idem=True))
    return c, key, uid


def done(c, served, key, rc=None, out=OUT, **extra):
    args = {"task": f"{key}/T1", **extra}
    files = None
    if rc is not None:
        args["receipt"], args["receipt_output"] = rc, "-"
        files = {"receipt_output": b64(out)} if out is not None else None
    return c.req("task.done", args, grant=served.grant, session=SESSION, idem=True, files=files)


def types(served, uid) -> list[str]:
    return [e["type"] for e in served.lab.ws.read_events(uid)]


def test_run_true_is_refused_by_the_host_and_nothing_is_appended(served):
    c, key, uid = started(served)
    res = done(c, served, key, run=True)
    assert err(res) == "op.not_allowed" and "never executes" in res["error"]["message"]
    assert "task.done" not in types(served, uid)


def test_a_receipt_for_another_cmd_is_refused(served):
    c, key, uid = started(served)
    assert err(done(c, served, key, receipt(cmd="true"))) == "invalid.input"
    assert err(done(c, served, key, receipt(cmd=CMD + " "))) == "invalid.input"  # exact, not trimmed
    assert "task.done" not in types(served, uid)


def test_a_receipt_for_a_task_without_verify_is_refused(served):
    c, key, uid = started(served, verify=None)
    assert err(done(c, served, key, receipt())) == "invalid.input"
    assert "task.done" not in types(served, uid)


def test_a_failed_run_is_not_a_receipt(served):
    c, key, uid = started(served)
    assert err(done(c, served, key, receipt(exit=1))) == "verify.failed"
    assert "task.done" not in types(served, uid)


def test_the_output_must_come_through_files(served):
    c, key, uid = started(served)
    g = served.grant
    assert err(done(c, served, key, receipt(), out=None)) == "blob.invalid"  # x-file without its entry
    only = c.req("task.done", {"task": f"{key}/T1", "receipt": receipt()}, grant=g, session=SESSION, idem=True)
    assert err(only) == "invalid.input"  # a receipt without its output
    bare = c.req("task.done", {"task": f"{key}/T1", "receipt_output": "-"}, grant=g, session=SESSION, idem=True,
                 files={"receipt_output": b64(OUT)})  # fmt: skip
    assert err(bare) == "invalid.input"  # an output without a receipt
    assert "task.done" not in types(served, uid)


def test_a_bad_receipt_shape_is_invalid_input(served):
    c, key, uid = started(served)
    for bad in (
        receipt(commit="abc"),
        receipt(exit=300),
        receipt(duration_ms=-1),
        {**receipt(), "attested_by": "agent"},
    ):
        assert err(done(c, served, key, bad)) == "invalid.input"
    assert "task.done" not in types(served, uid)


def test_a_valid_receipt_is_stored_labelled_with_its_output_as_a_receipt_artifact(served):
    c, key, uid = started(served)
    ok(done(c, served, key, receipt()))
    events = served.lab.ws.read_events(uid)
    ev = next(e for e in events if e["type"] == "task.done")
    assert ev["receipt"] == {"cmd": CMD, "exit": 0, "ms": 1234, "repo": None, "commit": None, "attested_by": "agent"}
    assert ev["log"] == "T1-receipt.log"
    art = next(e for e in events if e["type"] == "artifact.added")
    assert (art["name"], art["kind"], art["task"], art["sha256"]) == (
        "T1-receipt.log", "receipt", "T1", "sha256:" + hashlib.sha256(OUT).hexdigest()
    )  # fmt: skip
    got = ok(c.req("artifact.get", {"ref": key, "name": "T1-receipt.log", "offset": 0}, grant=served.grant))["data"]
    assert base64.b64decode(got["b64"]) == OUT


def test_the_read_shape_carries_the_label_and_never_a_host_claim(served):
    c, key, uid = started(served)
    before = ok(c.req("ticket.get", {"ref": key}))["data"]["tasks"][0]
    assert before["leased_by"] == SESSION and before["receipt"] is None
    ok(done(c, served, key, receipt()))
    data = ok(c.req("ticket.get", {"ref": key}))["data"]
    task = data["tasks"][0]
    assert task["state"] == "done" and task["leased_by"] is None
    r = task["receipt"]
    assert r["attested_by"] == "agent" and r["log"] == "T1-receipt.log"
    assert (r["cmd"], r["exit"], r["ms"], r["evidence"]) == (CMD, 0, 1234, True)
    assert "verified" not in str(r)
    item = next(i for i in data["log"] if i["type"] == "task.done")
    assert item["data"]["receipt"]["attested_by"] == "agent"


def test_a_receipt_with_an_unlinked_repo_is_refused(served):
    c, key, uid = started(served)
    res = done(c, served, key, receipt(repo="nope", commit="a" * 40))
    assert res["ok"] is False and res["error"]["code"] == "invalid.input" and "links no repo" in res["error"]["message"]
    assert "task.done" not in types(served, uid)


def test_batch_task_done_is_served_but_a_run_item_is_invalid(served):
    c, key, uid = started(served)
    batch = {"ref": key, "ops": [{"op": "task.done", "task": "T1", "run": True}]}
    res = c.req("apply", {"file": "b.json"}, grant=served.grant, session=SESSION, idem=True,
                files={"file": b64(json.dumps(batch).encode())})  # fmt: skip
    assert err(res) == "invalid.input"
    assert "task.done" not in types(served, uid)


def test_no_receipt_or_output_leaks_for_a_restricted_ticket(served):
    c, key, uid = started(served)
    ok(done(c, served, key, receipt()))
    served.restrict(uid, [served.lab.owner])
    as_agent_class(served)
    n = served.client()
    # no-person (N): the ticket, its tasks, its receipt and the output are all not_found
    assert err(n.req("ticket.get", {"ref": key})) == "not_found"
    assert err(n.req("artifact.get", {"ref": key, "name": "T1-receipt.log", "offset": 0})) == "not_found"
    assert key not in json.dumps(n.req("ticket.query", {}))
    res = n.req("task.done", {"task": f"{key}/T1", "receipt": receipt(), "receipt_output": "-"}, idem=True,
                files={"receipt_output": b64(OUT)})  # fmt: skip
    assert res["ok"] is False and key not in json.dumps(res).replace(f"{key}/T1", "")


# --- review round (Opus F1, F2, F5; Sonnet test gaps) ---

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'
COMMIT = "a" * 40


def linked(served: Served, verify: str | None = CMD):
    """Like :func:`started`, on a ticket that links the registered repo ``proj``."""
    served.lab.ws.plan_gate_off()
    uid, key = served.ticket("receipts")
    c, g = served.client(), served.grant
    served.append(lambda store: served.lab.ws.repo("proj"))
    ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
    ok(c.req("set", {"ref": key, "pairs": [LINKS]}, grant=g, session=SESSION, idem=True))
    ok(c.req("task.add", {"ref": key, "text": "do it", "verify": verify}, grant=g, session=SESSION, idem=True))
    ok(c.req("task.start", {"task": f"{key}/T1"}, grant=g, session=SESSION, idem=True))
    return c, key, uid


def test_f1_a_null_repo_is_refused_on_a_ticket_that_links_a_repo(served):
    c, key, uid = linked(served)
    for bad in (receipt(), receipt(commit=COMMIT), receipt(repo="proj"), receipt(repo="other", commit=COMMIT)):
        assert err(done(c, served, key, bad)) == "invalid.input"
    assert "task.done" not in types(served, uid)


def test_f1_a_receipt_names_a_linked_repo_and_its_commit_and_is_stored_as_sent(served):
    c, key, uid = linked(served)
    ok(done(c, served, key, receipt(repo="proj", commit=COMMIT)))
    ev = next(e for e in served.lab.ws.read_events(uid) if e["type"] == "task.done")
    assert ev["receipt"]["repo"] == "proj" and ev["receipt"]["commit"] == COMMIT
    shown = ok(c.req("ticket.get", {"ref": key}))["data"]["tasks"][0]["receipt"]
    assert (shown["repo"], shown["commit"], shown["attested_by"]) == ("proj", COMMIT, "agent")
    assert shown["evidence"] is False  # not the source sha: a client receipt follows the source


def test_f5_a_commit_without_a_repo_is_refused_on_a_ticket_that_links_none(served):
    c, key, uid = started(served)
    assert err(done(c, served, key, receipt(commit=COMMIT))) == "invalid.input"
    assert "task.done" not in types(served, uid)


def test_f2_two_file_arguments_with_one_name_are_refused(served):
    c, key, uid = started(served)
    args = {"task": f"{key}/T1", "receipt": receipt(), "receipt_output": "x.log", "artifact": "x.log"}
    files = {"receipt_output": b64(b"RECEIPT"), "artifact": b64(b"EVIDENCE")}
    res = c.req("task.done", args, grant=served.grant, session=SESSION, idem=True, files=files)
    assert err(res) == "blob.invalid"
    assert "task.done" not in types(served, uid)


def test_a_receipt_output_over_one_mib_is_refused_and_exactly_one_mib_is_taken(served):
    from tests.host.test_blobs import upload

    c, key, uid = started(served)
    big = upload(c, b"x" * ((1 << 20) + 1), grant=served.grant)
    args = {"task": f"{key}/T1", "receipt": receipt(), "receipt_output": "-"}
    res = c.req(
        "task.done", args, grant=served.grant, session=SESSION, idem=True, files={"receipt_output": {"blob": big}}
    )
    assert err(res) == "invalid.input"
    assert "task.done" not in types(served, uid)
    exact = upload(c, b"x" * (1 << 20), grant=served.grant)
    res = c.req(
        "task.done", args, grant=served.grant, session=SESSION, idem=True, files={"receipt_output": {"blob": exact}}
    )
    assert ok(res)


def test_a_dry_run_with_a_receipt_appends_nothing(served):
    c, key, uid = started(served)
    args = {"task": f"{key}/T1", "receipt": receipt(), "receipt_output": "-"}
    res = c.req(
        "task.done", args, grant=served.grant, session=SESSION, dry_run=True, files={"receipt_output": b64(OUT)}
    )
    assert res["ok"] is True
    assert "task.done" not in types(served, uid)


def test_r10_p02_the_host_refuses_a_task_start_while_the_plan_is_open_and_says_wait(served):
    """r10-P02 (run10 AT-0006): the agent built before the plan was approved. The host refuses it and says wait."""
    uid, key = served.ticket("plan open")
    c, g = served.client(), served.grant
    ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
    ok(c.req("task.add", {"ref": key, "text": "do it", "verify": CMD}, grant=g, session=SESSION, idem=True))
    res = c.req("task.start", {"task": f"{key}/T1"}, grant=g, session=SESSION, idem=True)
    e = res["error"]
    assert e["code"] == "transition.refused" and "plan is not approved" in e["message"] and e["hint"] == "orch wait"
    assert err(done(c, served, key, receipt())) == "transition.refused"
    assert not {"task.started", "task.done", "artifact.added"} & set(types(served, uid))
