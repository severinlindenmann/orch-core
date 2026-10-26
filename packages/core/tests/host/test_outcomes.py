"""H2b through the H2a ``OpBackend`` seam: real workspace sockets, a real store, the outcome store behind them."""

from __future__ import annotations

import errno
import json
import os

import pytest

from orch.host.control_client import new_idem
from tests.host.lifecycle_helpers import UID, Lab
from tests.host.outcome_helpers import OutcomeBackend, WsClient

IDEM = "a_01J9ZP0000000000000000000A"
IDEM2 = "a_01J9ZP0000000000000000000B"


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def ok(res):
    assert res["t"] == "res" and res["ok"] is True, res.get("error", res)
    return res["result"]


def err(res):
    assert res["t"] == "res" and res["ok"] is False, res
    return res["error"]["code"]


class Served:
    def __init__(self, lab, backend=None, **kw):
        self.lab = lab
        self.backend = backend or OutcomeBackend(lab.ws.agent, lab.owner)
        self.h = lab.host(backend=lambda: self.backend, clock=lambda: lab.ws.clock[0], **kw)
        self.h.__enter__()
        self.control = self.h.client()
        self.wid = ok(lab.register(self.control))["data"]["workspace_id"]
        self.service = self.h.host.services[self.wid]
        self.clients = []

    def client(self):
        ws = WsClient(self.lab.run / self.wid / "host.sock", self.wid, UID)
        self.clients.append(ws)
        return ws

    def tickets(self):
        """The ticket logs in the workspace root (read from disk: independent of the host's store)."""
        tickets = os.path.join(self.lab.root, ".orch", "tickets")
        return sorted(os.listdir(tickets)) if os.path.isdir(tickets) else []

    def close(self):
        self.backend.gate.set()
        for ws in self.clients:
            ws.close()
        self.control.close()
        self.h.stop()


@pytest.fixture
def served(lab):
    s = Served(lab)
    try:
        yield s
    finally:
        s.close()


def new(ws, idem=IDEM, title="x", request_id="r", **extra):
    return ws.call("new", {"title": title}, request_id=request_id, opts={"idem": idem}, **extra)


# --- replay, re-enveloping, conflict --------------------------------------------------------------------------------


def test_a_replay_is_a_duplicate_enveloped_with_the_retrys_request_id(served):
    ws = served.client()
    first = new(ws, request_id="first-1")
    assert first["id"] == "first-1" and ok(first)["duplicate"] is False
    retry = new(ws, request_id="retry-2")
    assert retry["id"] == "retry-2" and ok(retry)["duplicate"] is True
    assert ok(retry)["data"] == ok(first)["data"] and ok(retry)["seq"] == ok(first)["seq"]
    assert len(served.tickets()) == 1
    # the stored record holds a result, never the first frame or its request id
    record = (served.service.outcomes.dir / served.service.outcomes.key(served.wid, f"uid:{UID}", IDEM)).read_text()
    assert "first-1" not in record and '"t":"res"' not in record


def test_the_same_idem_with_another_binding_is_idem_conflict(served):
    ws = served.client()
    new(ws)
    assert err(new(ws, title="other")) == "idem.conflict"
    assert err(ws.call("new", {"title": "x"}, opts={"idem": IDEM}, auth={"session": "s_other"})) == "idem.conflict"
    assert err(ws.call("log", {"title": "x"}, opts={"idem": IDEM})) == "idem.conflict"
    assert len(served.tickets()) == 1


def test_a_write_without_an_idem_is_idem_required(served):
    ws = served.client()
    res = ws.call("new", {"title": "x"})
    assert err(res) == "idem.required" and res["exit"] == 5


def test_a_nonretryable_refusal_is_stored_and_a_retryable_failure_runs_again(served):
    ws = served.client()
    res = ws.call("log", opts={"idem": IDEM})
    assert err(res) == "invalid.input"
    served.backend.applied = 0
    again = ws.call("log", opts={"idem": IDEM})
    assert err(again) == "invalid.input" and served.backend.applied == 0  # the stored error, not a second run
    served.backend.busy = 2
    for _ in range(2):
        assert err(ws.call("ask", {"title": "b"}, opts={"idem": IDEM2})) == "lock.busy"  # retryable: nothing stored
    assert served.backend.applied == 2  # each retry ran again
    done = ws.call("ask", {"title": "b"}, opts={"idem": IDEM2})
    assert ok(done)["duplicate"] is False and len(served.tickets()) == 1 and served.backend.applied == 3
    assert ok(ws.call("ask", {"title": "b"}, opts={"idem": IDEM2}))["duplicate"] is True


def test_concurrent_duplicates_on_two_connections_get_one_append(served):
    a, b = served.client(), served.client()
    a.send("a1", "new", {"title": "x"}, opts={"idem": IDEM})
    b.send("b1", "new", {"title": "x"}, opts={"idem": IDEM})
    results = [ok(a.read()), ok(b.read())]
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert results[0]["data"] == results[1]["data"]
    assert len(served.tickets()) == 1 and served.backend.applied == 1


# --- outcome.get ----------------------------------------------------------------------------------------------------


def test_outcome_get_reports_the_stored_state(served):
    ws = served.client()
    first = ok(new(ws))
    got = ok(ws.call("outcome.get", {"idem": IDEM}))["data"]
    assert got == {"state": "committed", "result": first, "exit": 0}
    ws.call("log", opts={"idem": IDEM2})
    failed = ok(ws.call("outcome.get", {"idem": IDEM2}))["data"]
    assert failed["state"] == "failed" and failed["error"]["code"] == "invalid.input" and failed["exit"] == 5
    assert err(ws.call("outcome.get", {"idem": "a_01J9ZP0000000000000000000C"})) == "outcome.unknown_idem"


def second_grant(lab):
    lab.ws.store = lab.ws.other()
    try:
        return lab.ws.narrow_grant(["new"])
    finally:
        lab.ws.store.close()
        lab.ws.store = None


def test_outcome_get_is_scoped_to_the_principal_and_a_wrong_secret_gets_nothing(lab):
    other = second_grant(lab)
    s = Served(lab)
    try:
        ws = s.client()
        good = lab.ws.grant
        first = ok(ws.call("new", {"title": "g"}, opts={"idem": IDEM}, auth={"grant": good}))
        assert ok(ws.call("outcome.get", {"idem": IDEM}, auth={"grant": good}))["data"]["result"] == first
        gid = good.split(".")[0]
        wrong = f"{gid}." + "A" * 43  # that grant's id, another secret (F4)
        assert err(ws.call("outcome.get", {"idem": IDEM}, auth={"grant": wrong})) == "outcome.unknown_idem"
        assert err(ws.call("outcome.get", {"idem": IDEM}, auth={"grant": other})) == "outcome.unknown_idem"
        assert err(ws.call("outcome.get", {"idem": IDEM})) == "outcome.unknown_idem"  # the UID itself has none
        # the same idem under the UID's own principal is an independent record
        own = ok(new(ws, title="own"))
        assert own["duplicate"] is False and own["data"] != first["data"]
        # and a wrong secret on a write is grant.expired, before the outcome store is reached
        assert err(ws.call("new", {"title": "g"}, opts={"idem": IDEM}, auth={"grant": wrong})) == "grant.expired"
    finally:
        s.close()


# --- cancel after commit --------------------------------------------------------------------------------------------


def test_cancel_after_commit_never_undoes_the_write_and_outcome_get_reports_it(served):
    ws, ws2 = served.client(), served.client()
    ws.send("w", "artifact.add", {"title": "slow"}, opts={"idem": IDEM})
    assert served.backend.in_apply.wait(10)  # the event is appended; the call is still running
    ws.cancel("w")
    res = ws.read()
    assert res["id"] == "w" and err(res) == "outcome.unknown" and res["error"]["retryable"] is True
    served.backend.gate.set()  # the write finishes and commits although its caller was told outcome.unknown
    got = None
    for _ in range(100):
        got = ok(ws2.call("outcome.get", {"idem": IDEM}))["data"]
        if got["state"] != "pending":
            break
    assert got["state"] == "committed" and len(served.tickets()) == 1
    retry = ok(ws.call("artifact.add", {"title": "slow"}, opts={"idem": IDEM}, request_id="again"))
    assert retry["duplicate"] is True and retry["data"] == got["result"]["data"]


def test_a_connection_dropped_mid_write_still_commits_the_record(served):
    ws = served.client()
    ws.send("w", "artifact.add", {"title": "slow"}, opts={"idem": IDEM})
    assert served.backend.in_apply.wait(10)
    ws.close()
    served.backend.gate.set()
    ws2 = served.client()
    for _ in range(100):
        got = ok(ws2.call("outcome.get", {"idem": IDEM}))["data"]
        if got["state"] != "pending":
            break
    assert got["state"] == "committed"


def test_a_cancelled_queued_write_is_cancelled_and_leaves_no_record(served):
    ws, ws2 = served.client(), served.client()
    ws.send("slow", "artifact.add", {"title": "slow"}, opts={"idem": IDEM})
    assert served.backend.in_apply.wait(10)
    ws2.send("q", "new", {"title": "queued"}, opts={"idem": IDEM2})
    ws2.cancel("q")
    res = ws2.read()
    assert res["id"] == "q" and err(res) == "cancelled"  # it never started: nothing to commit
    served.backend.gate.set()
    assert ok(ws.read())["duplicate"] is False
    assert err(ws2.call("outcome.get", {"idem": IDEM2})) == "outcome.unknown_idem"
    assert len(served.tickets()) == 1


# --- dry_run --------------------------------------------------------------------------------------------------------


def test_a_dry_run_neither_replays_nor_answers_idem_errors(served):
    ws = served.client()
    first = ok(new(ws))
    # same key: a stored result exists, a dry run does not replay it
    dry = ok(ws.call("new", {"title": "x"}, opts={"idem": IDEM, "dry_run": True}))
    assert dry["data"] == {"dry_run": True, "title": "x"} and dry["duplicate"] is False
    assert dry["data"] != first["data"]
    # another binding on the same key: not idem.conflict
    assert ok(ws.call("new", {"title": "y"}, opts={"idem": IDEM, "dry_run": True}))["data"]["title"] == "y"
    # no idem at all: not idem.required
    assert ok(ws.call("new", {"title": "z"}, opts={"dry_run": True}))["data"]["title"] == "z"
    # an expired key: not idem.expired
    served.lab.ws.clock[0] += 25 * 3600
    served.service.submit(served.service.outcomes.sweep).result(10)
    assert err(ws.call("outcome.get", {"idem": IDEM})) == "idem.expired"
    assert ok(ws.call("new", {"title": "x"}, opts={"idem": IDEM, "dry_run": True}))["data"]["dry_run"] is True
    assert len(served.tickets()) == 1  # nothing appended, nothing recorded
    assert len(os.listdir(served.service.outcomes.dir)) == 1


def test_dry_run_on_a_read_is_invalid_input(served):
    ws = served.client()
    res = ws.call("show", opts={"dry_run": True})
    assert err(res) == "invalid.input" and "dry_run is not supported by show" in res["error"]["message"]


def test_dry_run_on_a_control_op_is_invalid_input_and_a_control_write_needs_its_idem(lab):
    from orch.host.control_client import ControlClient

    with lab.host() as h, h.client() as c:
        for op, args in (
            ("host.list", {}),
            ("host.register", {"root": lab.root, "peers": lab.peers()}),
            ("host.outcome", {"idem": IDEM}),
            ("host.wsk_create", {}),
        ):
            res = c.call(op, args, idem=new_idem(), opts={"dry_run": True})
            assert err(res) == "invalid.input" and "dry_run is not supported" in res["error"]["message"], op
        assert err(c.call("host.register", {"root": lab.root, "peers": lab.peers()})) == "idem.required"
        assert ControlClient  # the client class is the one H2a's tests use


# --- tombstones over the socket -------------------------------------------------------------------------------------


def test_outcome_get_and_a_retry_on_a_tombstone_answer_idem_expired(served):
    ws = served.client()
    new(ws)
    served.lab.ws.clock[0] += 24 * 3600 + 1
    served.service.submit(served.service.outcomes.sweep).result(10)
    assert err(ws.call("outcome.get", {"idem": IDEM})) == "idem.expired"
    res = new(ws)
    assert err(res) == "idem.expired" and res["exit"] == 2
    assert len(served.tickets()) == 1  # never a second append
    assert err(new(ws, title="other")) == "idem.conflict"


# --- the {digest} rules over the socket -----------------------------------------------------------------------------


def test_files_binding_through_blob_then_digest_and_digest_without_a_record(served):
    import base64
    import hashlib

    ws = served.client()
    data = b"payload"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    b64 = base64.b64encode(data).decode()
    res = ws.call("new", {"title": "f"}, opts={"idem": IDEM}, files={"file": {"digest": digest}})
    assert err(res) == "blob.invalid"  # no record yet
    first = ok(ws.call("new", {"title": "f"}, opts={"idem": IDEM}, files={"file": {"b64": b64}}))
    retry = ws.call("new", {"title": "f"}, opts={"idem": IDEM}, files={"file": {"digest": digest}})
    assert ok(retry)["duplicate"] is True and ok(retry)["data"] == first["data"]
    other = "sha256:" + "0" * 64
    assert (
        err(ws.call("new", {"title": "f"}, opts={"idem": IDEM}, files={"file": {"digest": other}})) == "idem.conflict"
    )
    served.backend.blobs["b_" + "A" * 26] = digest
    by_blob = ws.call("new", {"title": "f"}, opts={"idem": IDEM}, files={"file": {"blob": "b_" + "A" * 26}})
    assert ok(by_blob)["duplicate"] is True  # the same binding by any of the three spellings
    assert (
        err(ws.call("new", {"title": "f"}, opts={"idem": IDEM2}, files={"file": {"blob": "b_" + "B" * 26}}))
        == "blob.invalid"
    )


# --- the stored record is private and host-owned --------------------------------------------------------------------


def test_outcome_records_are_private_files_of_the_host(served):
    ws = served.client()
    new(ws)
    directory = served.service.outcomes.dir
    assert directory == __import__("pathlib").Path(served.lab.state) / "hosts" / served.wid / "outcomes"
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    (name,) = os.listdir(directory)
    assert oct((directory / name).stat().st_mode & 0o777) == "0o600"
    record = json.loads((directory / name).read_text())
    assert record["planned"][0]["log"] == record["result"]["data"]["uid"]


# --- retention in a host that stays up ------------------------------------------------------------------------------


def test_a_running_host_sweeps_by_itself_without_a_reopen(lab):
    import time

    s = Served(lab, sweep_interval=0.2)
    try:
        ws = s.client()
        new(ws)
        assert ok(ws.call("outcome.get", {"idem": IDEM}))["data"]["state"] == "committed"
        lab.ws.clock[0] += 24 * 3600 + 1  # a day passes; nothing reopens the workspace
        deadline = time.monotonic() + 15
        code = None
        while time.monotonic() < deadline:
            res = ws.call("outcome.get", {"idem": IDEM})
            code = None if res["ok"] else res["error"]["code"]
            if code == "idem.expired":
                break
            time.sleep(0.1)
        assert code == "idem.expired"
        lab.ws.clock[0] += 7 * 24 * 3600 + 1  # and the tombstone goes after its 7 days
        for _ in range(150):
            if not os.listdir(s.service.outcomes.dir):
                break
            time.sleep(0.1)
        assert os.listdir(s.service.outcomes.dir) == []
        assert err(ws.call("outcome.get", {"idem": IDEM})) == "outcome.unknown_idem"
    finally:
        s.close()
