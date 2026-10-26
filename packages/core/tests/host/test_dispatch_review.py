"""H4a1 review round (#383): doctor --repair refused, unserved ops unreachable through apply, check filtered for the
viewer, errors and rebuilt results redacted before they are stored, the deny-all append check, no host state dir for
handlers."""

from __future__ import annotations

import base64
import errno
import json
import os

import pytest

import orch.ops as ops
from orch.host.auth import GRANT_SHAPE
from orch.host.dispatch import NOT_SERVED, rebuild, served_ops
from orch.ops import Result
from orch.ops.errors import OrchError
from orch.ops.plans import batch_ops
from tests.host.dispatch_helpers import SESSION, Served, err, ok
from tests.host.lifecycle_helpers import Lab
from tests.host.test_dispatch_matrix import as_agent_class

GRANTISH = "gr_01J9ZP0000000000000000000Q." + "Q" * 42 + "A"
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


@pytest.fixture
def served(lab):
    s = Served(lab)
    try:
        yield s
    finally:
        s.close()


def b64(obj):
    return {"b64": base64.b64encode(json.dumps(obj).encode()).decode()}


def edit_ticket_json(served, uid):
    path = os.path.join(served.lab.root, ".orch", "tickets", uid, "ticket.json")
    with open(path, "a") as f:
        f.write(" ")


# --- 1. doctor --repair ---------------------------------------------------------------------------------------------


def test_doctor_repair_is_refused_and_appends_nothing(served):
    uid, _ = served.ticket("x")
    before = served.lab.ws.read_events(uid)
    edit_ticket_json(served, uid)
    c = served.client()  # column P: the mapped owner
    res = c.req("doctor", {"repair": True})
    assert err(res) == "op.not_allowed" and "control" in res["error"]["message"]
    assert served.lab.ws.read_events(uid) == before
    plain = ok(c.req("doctor"))  # the read-only doctor still runs, on the host's own store
    assert "projection.ticket" in json.dumps(plain)
    assert served.lab.ws.read_events(uid) == before


def test_read_only_doctor_and_check_open_no_second_store_and_read_no_config(served, monkeypatch):
    import orch.ops.health as health
    import orch.ops.runtime as runtime

    def refuse(*a, **k):
        raise AssertionError("a second store or a config.json read in host mode")

    monkeypatch.setattr(health.InspectStore, "open", classmethod(refuse))
    monkeypatch.setattr(runtime, "_config", refuse)  # doctor and check read the config only via Workspace.config
    c = served.client()
    assert ok(c.req("doctor"))["data"]["problems"] >= 0
    assert ok(c.req("check"))["data"]["problems"] >= 0


# --- 2. unserved ops through apply ----------------------------------------------------------------------------------


def test_every_batch_op_is_served_or_refused_by_the_host():
    batch = set(batch_ops())
    assert batch - set(served_ops()) <= set(NOT_SERVED)
    assert "task.done" in batch  # the alias the review found


def test_task_done_is_served_at_contract_2_and_a_batch_item_cannot_run_a_command(served):
    g, c = served.grant, served.client()
    uid, key = served.ticket("t")
    ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
    ok(c.req("task.add", {"ref": key, "text": "do it", "verify": "make test"}, grant=g, session=SESSION, idem=True))
    batch = {"ref": key, "ops": [{"op": "task.done", "task": "T1", "run": True}]}
    res = c.req("apply", {"file": "b.json"}, grant=g, session=SESSION, idem=True, files={"file": b64(batch)})
    assert err(res) == "invalid.input"  # served, but a batch item has no run
    assert "task.done" not in [e["type"] for e in served.lab.ws.read_events(uid)]
    fine = {"ref": key, "ops": [{"op": "log", "text": "a batch note"}]}
    assert ok(c.req("apply", {"file": "b.json"}, grant=g, session=SESSION, idem=True, files={"file": b64(fine)}))


# --- 3. check for the viewer ------------------------------------------------------------------------------------------


def test_check_gives_no_person_no_restricted_key_and_no_count_of_it(served):
    open_uid, open_key = served.ticket("open one")
    secret_uid, secret_key = served.ticket("secret one")
    served.restrict(secret_uid, [served.lab.owner])
    edit_ticket_json(served, secret_uid)
    p = ok(served.client().req("check"))  # column P (the owner) sees it
    assert secret_key in json.dumps(p)
    as_agent_class(served)
    n = served.client().req("check")
    body = json.dumps(n)
    assert secret_key not in body and secret_uid not in body
    assert ok(n)["data"]["problems"] == p["data"]["problems"] - 1
    edit_ticket_json(served, open_uid)
    assert open_key in json.dumps(served.client().req("check"))  # a visible ticket's finding stays


# --- 4. redaction before the record -----------------------------------------------------------------------------------


def _records(served) -> str:
    d = served.lab.state / "hosts" / served.wid / "outcomes"
    return "".join(p.read_text() for p in d.iterdir())


def test_a_refusal_is_redacted_before_it_is_stored(served):
    g = served.grant
    secret = g.partition(".")[2]
    original = ops.get("log").handler

    def refuses(ctx, args):
        raise OrchError("quota.unattended", f"leak {ctx.grant} {GRANTISH}", hint=f"see {secret} {PEM}")

    ops.bind("log", refuses)
    try:
        res = served.client().req("log", {"text": "x"}, grant=g, session=SESSION, idem=True)
    finally:
        ops.bind("log", original)
    assert err(res) == "quota.unattended"
    for text in (json.dumps(res), _records(served)):
        assert secret not in text and not GRANT_SHAPE.search(text) and "PRIVATE KEY" not in text


def test_logged_grant_shaped_text_never_reaches_a_response(served):
    """Text written into the log outside the host (direct mode, a forged line) comes back redacted."""
    uid, key = served.ticket("x")
    ws = served.lab.ws

    def plant(store):
        store.ticket(uid)
        store.append({"type": "log.added", "actor": ws.agent, "text": f"{GRANTISH} {PEM}"}, log=uid)

    served.append(plant)
    c = served.client()
    for op, args in [("show", {"ref": key}), ("show", {"ref": key, "log": True}), ("search", {"query": "QQQQ"})]:
        res = c.req(op, args, grant=served.grant)
        body = json.dumps(res)
        assert not GRANT_SHAPE.search(body) and "PRIVATE KEY" not in body, (op, body[:300])


def test_a_rebuilt_result_is_redacted():
    created = {"type": "ticket.created", "seq": 1, "key": "DEMO-0001", "title": f"t {GRANTISH}", "ticket_type": "bug"}
    out = json.dumps(rebuild({"op": "new"}, [created]))
    assert not GRANT_SHAPE.search(out)


# --- 5. the deny-all append check -------------------------------------------------------------------------------------


def test_a_read_or_a_dry_run_cannot_append(served):
    uid, key = served.ticket("x")
    original = ops.get("show").handler

    def appends(ctx, args):
        from orch.ops.runtime import Call

        c = Call.of(ctx, "show")
        c.store.append({"type": "log.added", "actor": served.lab.ws.agent, "text": "from a read"}, log=uid)
        return Result(data={})

    ops.bind("show", appends)
    try:
        res = served.client().req("show", {"ref": key}, grant=served.grant, session=SESSION)
    finally:
        ops.bind("show", original)
    assert err(res) == "internal"
    assert "from a read" not in json.dumps(served.lab.ws.read_events(uid))
    ok(served.client().req("new", {"title": "dry"}, grant=served.grant, session=SESSION, dry_run=True))
    assert served.service.store.append_check is not None


# --- 6. no host state dir for handlers -------------------------------------------------------------------------------


def test_status_verbose_returns_no_host_path_and_handlers_have_only_the_root(served):
    as_agent_class(served)
    res = ok(served.client().req("status", {"verbose": True}))
    assert str(served.service.state_dir) not in json.dumps(res) and "state_dir" not in res["data"]
    caps = served.service.caps
    assert caps is not None and [str(r) for r in caps.files.roots] == [str(served.service.entry.store_dir)]
    assert "ORCH_STATE_DIR" not in caps.env and "ORCH_WORKSPACE" not in caps.env
    keys = served.service.state_dir / "hosts" / served.wid / "keys"
    with pytest.raises(OSError):
        caps.files.read_bytes(next(p for p in keys.rglob("*") if p.is_file()))


def test_a_person_in_column_p_is_the_viewer_of_list_and_status(served):
    uid, key = served.ticket("mine")
    served.restrict(uid, [served.lab.owner])
    c = served.client()
    assert ok(c.req("status"))["data"]["person"] != "unattended"
    assert key in json.dumps(ok(c.req("list")))


def test_host_mode_doctor_reads_files_only_through_the_capability(served, monkeypatch):
    """Health's file reads in host mode go through caps.files (roots, fd walk, no links). Since #516 health has no
    path read at all (direct mode reads below the store's pinned root), so there is no read_or_none left to refuse."""
    import orch.ops.health as health

    uid, _ = served.ticket("x")
    edit_ticket_json(served, uid)
    assert not hasattr(health, "read_or_none")
    seen = []
    real = health._read

    def spy(files, path, limit=None):
        seen.append(type(files).__name__)
        return real(files, path, limit)

    monkeypatch.setattr(health, "_read", spy)
    res = ok(served.client().req("doctor"))
    assert seen and set(seen) == {"Files"}  # the host's capability, never the direct-mode root
    assert "projection.ticket" in json.dumps(res)


def test_hosted_doctor_and_check_with_several_ticket_checkpoints(served):
    """The checkpoint-file loop meets several ticket checkpoints (the visibility test is never shadowed), and a bad
    checkpoint of a restricted ticket is a finding for its viewer only."""
    from pathlib import Path

    uids = [served.ticket(f"t{i}")[0] for i in range(3)]
    served.restrict(uids[0], [served.lab.owner])
    cdir = Path(served.lab.root) / ".orch" / ".state" / "checkpoints"
    named = [p for p in cdir.glob("ticket-*.json") if p.stem.removeprefix("ticket-") in uids]
    assert len(named) >= 3
    (cdir / f"ticket-{uids[0]}.json").write_text("{}")  # unreadable: checkpoint.bad
    (cdir / f"ticket-{uids[1]}.json").write_text("{}")
    c = served.client()
    for op in ("doctor", "check"):
        body = json.dumps(ok(c.req(op)))
        assert f"ticket-{uids[0]}" in body and f"ticket-{uids[1]}" in body, op
    as_agent_class(served)
    n = served.client()
    body = json.dumps(ok(n.req("check")))
    assert uids[0] not in body and f"ticket-{uids[1]}" in body


# --- #381: host-mode git reads run under the caller's principal ----------------------------------------------------


class FakeReader:
    """A HostGitReader stand-in: records the principal of every read."""

    def __init__(self):
        self.principals = []
        self.synced = 0

    def sync(self, declared):
        self.synced += 1
        return {}

    def observe(self, path, refs, *, principal=None):
        self.principals.append(principal)
        return {"head_sha": "a" * 40, "refs": {}, "origin_urls": []}


def test_a_git_read_through_the_dispatcher_runs_under_the_callers_principal(served):
    from pathlib import Path

    from orch.ops.caps import Capabilities
    from tests.host.dispatch_helpers import UID

    _, key = served.ticket("x")
    service = served.service
    reader = FakeReader()

    def install():
        if service.caps is not None:
            service.caps.close()
        service.caps = Capabilities.host((service.entry.root,), env={}, clock=service.clock, git=reader)

    service.submit(install).result(10)
    assert service.caps.git.principal is None  # the workspace's caps carry no principal of their own
    original = ops.get("status").handler

    def reads_git(ctx, args):
        return Result(data={"head": ctx.caps.git.head(Path(ctx.workspace.root))})

    ops.bind("status", reads_git)
    try:
        c = served.client()
        ok(c.req("status", grant=served.grant, session=SESSION))
        ok(c.req("status"))
    finally:
        ops.bind("status", original)
    assert reader.principals == [f"g:{served.lab.ws.grant_id}@{UID}", f"uid:{UID}"]
    shown = ok(served.client().req("show", {"ref": key}, grant=served.grant))  # observe, through the real handler
    # pins are the workspace service's (at open and on a settings.changed, H10a), never taken by an op's observe
    assert reader.synced == 0 and "needs the caller's principal" not in json.dumps(shown)
    assert service.caps.git.principal is None  # bound per request on a copy, never on the shared caps
