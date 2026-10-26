"""H4a1: registry writes through H2b's outcome store (deferred plans, the append check, replays), timeouts, the order
of one connection's writes, and the H2a seams fixed here (unregister drains, the person check off the event loop)."""

from __future__ import annotations

import errno
import json
import threading
import time

import pytest

import orch.ops as ops
from orch.host.control_client import new_idem
from orch.ops import Result
from orch.ops.errors import OrchError
from tests.host.dispatch_helpers import SESSION, UID, Served, err, ok
from tests.host.lifecycle_helpers import Lab


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


def record_of(served: Served, idem: str) -> dict:
    principal = f"g:{served.lab.ws.grant_id}@{UID}"
    outcomes = served.service.outcomes
    return outcomes.read(outcomes.key(served.wid, principal, idem))


def hold(served: Served) -> threading.Event:
    """Block the workspace's worker thread until the returned event is set (no wall-clock margins)."""
    gate = threading.Event()
    started = threading.Event()

    def wait() -> None:
        started.set()
        gate.wait(60)

    served.service.submit(wait)
    started.wait(10)
    return gate


def in_flight(served: Served) -> list:
    server = served.h.host.servers[served.wid][0]
    return [t for s in server.sessions for t in s.tasks.values() if not t.done()]


def until(cond, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


class Rebind:
    def __init__(self, name, handler):
        self.name, self.handler, self.original = name, handler, ops.get(name).handler

    def __enter__(self):
        ops.bind(self.name, self.handler)

    def __exit__(self, *exc):
        ops.bind(self.name, self.original)


def test_a_write_plans_its_events_before_appending_and_replays_its_result(served):
    c, g = served.client(), served.grant
    idem = "a_01J9ZP0000000000000000000E"
    first = ok(c.req("new", {"title": "planned", "message": "a body"}, grant=g, session=SESSION, idem=idem))
    record = record_of(served, idem)
    assert record["step"] == "committed" and record["result"] == first
    types = [p["type"] for p in record["planned"]]
    assert types == ["ticket.created", "ticket.updated"]
    uid = record["planned"][0]["log"]
    logged = served.lab.ws.read_events(uid)
    assert [e["id"] for e in logged] == [p["id"] for p in record["planned"]]
    assert record["key"] == first["key"]
    again = ok(c.req("new", {"title": "planned", "message": "a body"}, grant=g, session=SESSION, idem=idem))
    assert again == {**first, "duplicate": True}
    assert err(c.req("new", {"title": "other"}, grant=g, session=SESSION, idem=idem)) == "idem.conflict"
    assert len(served.lab.ws.read_events(uid)) == 2
    # a projection-based op plans its batch the same way
    claim_idem = new_idem()
    ok(c.req("claim", {"ref": first["key"]}, grant=g, session=SESSION, idem=claim_idem))
    assert [p["type"] for p in record_of(served, claim_idem)["planned"]] == ["claim.taken"]


def test_a_dry_run_writes_no_record_and_appends_nothing(served):
    c, g = served.client(), served.grant
    idem = "a_01J9ZP0000000000000000000F"
    res = ok(c.req("new", {"title": "dry"}, grant=g, session=SESSION, idem=idem, dry_run=True))
    assert "dry-run: nothing was written" in res["lines"]
    assert record_of(served, idem) is None
    assert ok(c.req("list"))["data"]["count"] == 0


def test_an_append_the_record_does_not_list_is_refused(served):
    _, key = served.ticket()

    def sneaky(ctx, args):  # appends without announcing the event
        from orch.ops.runtime import Call

        c = Call.of(ctx, "log")
        view = c.resolve(args["ref"])
        c.store.append({"type": "log.added", "actor": c.actor(), "text": "unplanned"}, log=view.uid)
        return Result(data={})

    with Rebind("log", sneaky):
        res = served.client().req("log", {"text": "x", "ref": key}, grant=served.grant, session=SESSION, idem=True)
    assert err(res) == "internal"
    uid = served.lab.ws.uid(key)
    assert [e["type"] for e in served.lab.ws.read_events(uid)] == ["ticket.created"]


def test_a_refusal_after_the_append_is_not_believed_the_result_is_rebuilt_from_the_log(served):
    _, key = served.ticket()

    def lands_then_fails(ctx, args):
        from orch.ops.runtime import Call

        c = Call.of(ctx, "log")
        with c.locked():
            view = c.resolve(args["ref"])
            p = c.projection(view)
            p.add({"type": "log.added", "text": "landed"})
            p.commit()
        raise OrchError("quota.unattended", "refused after the append")

    idem = new_idem()
    with Rebind("log", lands_then_fails):
        res = served.client().req("log", {"text": "x", "ref": key}, grant=served.grant, session=SESSION, idem=idem)
    result = ok(res)
    assert result["key"] == key and result["seq"] == 2
    assert record_of(served, idem)["step"] == "committed"


def test_a_slow_read_times_out_and_a_slow_write_answers_outcome_unknown_then_commits(served):
    _, key = served.ticket()
    c, g = served.client(), served.grant
    gate = hold(served)  # the workspace's worker thread is busy until the gate opens
    try:
        assert err(c.req("list", timeout_ms=1000)) == "timeout"
        idem = new_idem()
        res = c.req("log", {"text": "late", "ref": key}, grant=g, session=SESSION, idem=idem, timeout_ms=1000)
        assert err(res) == "outcome.unknown" and res["error"]["retryable"] is True
    finally:
        gate.set()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = ok(c.req("outcome.get", {"idem": idem}, grant=g))["data"]["state"]
        if state == "committed":
            break
        time.sleep(0.1)
    assert state == "committed"
    assert ok(c.req("log", {"text": "late", "ref": key}, grant=g, session=SESSION, idem=idem))["duplicate"] is True


def test_one_connections_writes_land_in_the_order_they_were_received(served):
    _, key = served.ticket()
    c, g = served.client(), served.grant
    gate = hold(served)
    auth = {"grant": g, "session": SESSION}
    texts = [f"note {i}" for i in range(6)]
    try:
        for i, text in enumerate(texts):
            c.ws.send(f"w{i}", "log", {"text": text, "ref": key}, auth=auth, opts={"idem": new_idem()})
        until(lambda: len(in_flight(served)) == len(texts))
    finally:
        gate.set()
    answers = [c.ws.read(20) for _ in texts]
    assert all(a["ok"] for a in answers), [a.get("error") for a in answers]
    logged = [e["text"] for e in served.lab.ws.read_events(served.lab.ws.uid(key)) if e["type"] == "log.added"]
    assert logged == texts


def test_unregister_drains_a_write_in_flight_and_answers_it(served):
    _, key = served.ticket()
    c, g = served.client(), served.grant
    gate = hold(served)  # the write queues behind this on the worker thread
    c.ws.send("w", "log", {"text": "in flight", "ref": key}, auth={"grant": g, "session": SESSION},
              opts={"idem": new_idem()})  # fmt: skip
    until(lambda: len(in_flight(served)) == 1)
    done = {}

    def unregister():
        control = served.h.client()
        try:
            done["res"] = control.call("host.unregister", {"workspace_id": served.wid}, idem=new_idem())
        finally:
            control.close()

    t = threading.Thread(target=unregister)
    t.start()
    until(lambda: served.service.closing)  # the drain has begun, with the write still waiting on the worker
    gate.set()
    answer = c.ws.read(20)
    t.join(30)
    assert answer["id"] == "w" and answer["ok"] is True, answer
    assert ok(done["res"]) is not None
    assert "in flight" in json.dumps(served.lab.ws.read_events(served.lab.ws.uid(key)))


def test_a_busy_worker_thread_never_blocks_a_new_connections_handshake(served):
    gate = hold(served)  # blocked until released: a handshake that needed the worker thread would hang
    try:
        start = time.monotonic()
        c = served.client()
        assert time.monotonic() - start < 10
        assert c.welcome["peer"]["person"] == served.lab.owner  # from the fresh snapshot, not the worker thread
    finally:
        gate.set()


def test_a_stale_snapshot_is_never_trusted_for_a_refusal(served, lab):
    """A grant issued after the snapshot was built: the gate does not refuse from the stale snapshot, the worker
    thread's fresh check lets the write through."""
    _, key = served.ticket()
    ws = lab.ws
    ws.tick()

    def issue(store):
        store.append(ws.grant_event(), log="workspace")

    served.append(issue)  # a new grant for the owner (ws.grant now names it); the host's snapshot predates it
    assert served.service.auth.verify(ws.grant, ws.clock[0]).state != "verified"
    res = served.client().req("log", {"text": "fresh", "ref": key}, grant=ws.grant, session=SESSION, idem=True)
    assert ok(res)["key"] == key
