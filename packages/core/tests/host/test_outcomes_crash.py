"""H2b: kill -9 between the append and the record (a real second process), and the two H2a follow-ups."""

from __future__ import annotations

import json
import os
import sys

import pytest

from orch.host.control_client import ControlClient, new_idem
from orch.host.intents import IntentStore
from tests.host.lifecycle_helpers import UID, Lab, host_process, wait_for_socket
from tests.host.outcome_helpers import OutcomeBackend, WsClient
from tests.host.test_crash_recovery import bootstrap_args, crash, err, ok, wsk_create
from tests.identity.helpers import Person

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="Unix host")

IDEM = "a_01J9ZP0000000000000000000A"


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    finally:
        value.close()


def tickets(lab):
    path = os.path.join(lab.root, ".orch", "tickets")
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def outcome_files(lab, wid):
    directory = lab.state / "hosts" / wid / "outcomes"
    return [json.loads((directory / name).read_text()) for name in sorted(os.listdir(directory))]


def crash_write(lab, monkeypatch, point, op="new"):
    """A host process armed at ``point``: register the workspace, send one write, see the process die by SIGKILL."""
    monkeypatch.setenv("T_ACTOR", json.dumps(lab.ws.agent))
    monkeypatch.setenv("T_OWNER", lab.owner)
    monkeypatch.setenv("T_CLOCK", str(lab.ws.clock[0]))
    proc = host_process(lab.state, lab.run, crash=point, pins=lab.pins_dir)
    try:
        control = lab.run / "control" / "host.sock"
        wait_for_socket(control, proc)
        with ControlClient(control) as c:
            wid = ok(lab.register(c))["data"]["workspace_id"]
        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        ws.send("w1", op, {"title": "crash"}, opts={"idem": IDEM})
        try:
            res = ws.read(20)
        except (ConnectionError, OSError, AssertionError):
            pass
        else:
            proc.terminate()
            proc.wait(10)
            pytest.fail(f"the host answered instead of dying: {res}\n{proc.stderr.read().decode()[-3000:]}")
        assert proc.wait(20) == -9
        return wid
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def restarted(lab):
    return lab.host(backend=lambda: OutcomeBackend(lab.ws.agent, lab.owner), clock=lambda: lab.ws.clock[0])


def test_kill_between_append_and_record_then_the_retry_is_a_duplicate_rebuilt_from_the_log(lab, monkeypatch):
    wid = crash_write(lab, monkeypatch, "outcome-before-commit")
    assert len(tickets(lab)) == 1  # the append is in the log...
    (record,) = outcome_files(lab, wid)
    assert record["step"] == "pending" and len(record["planned"]) == 1  # ...and the record never got past pending
    with restarted(lab) as h:
        service = h.host.services[wid]
        # reconciled at start: the record is committed, its result rebuilt from the log
        (after,) = outcome_files(lab, wid)
        assert after["step"] == "committed"
        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        got = ok(ws.call("outcome.get", {"idem": IDEM}))["data"]
        assert got["state"] == "committed" and got["exit"] == 0
        uid = record["planned"][0]["log"]
        assert got["result"]["data"]["uid"] == uid and got["result"]["seq"] == 1
        retry = ws.call("new", {"title": "crash"}, request_id="retry-9", opts={"idem": IDEM})
        assert retry["id"] == "retry-9"
        assert ok(retry)["duplicate"] is True and ok(retry)["data"] == got["result"]["data"]
        assert tickets(lab) == [uid]  # exactly one append, ever
        assert service.outcomes.count(f"uid:{UID}") == 1
        ws.close()


def test_kill_after_the_record_but_before_the_append_removes_it_and_the_retry_runs_again(lab, monkeypatch):
    wid = crash_write(lab, monkeypatch, "outcome-before-append")
    assert tickets(lab) == []
    (record,) = outcome_files(lab, wid)
    assert record["step"] == "pending"
    with restarted(lab) as h:
        assert outcome_files(lab, wid) == []  # nothing landed, no lock holder left: the record is removed
        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        assert err(ws.call("outcome.get", {"idem": IDEM})) == "outcome.unknown_idem"
        retry = ok(ws.call("new", {"title": "crash"}, opts={"idem": IDEM}))
        assert retry["duplicate"] is False and len(tickets(lab)) == 1
        ws.close()
        assert h.host.services[wid].outcomes.count(f"uid:{UID}") == 1


def test_a_second_crash_after_reconciliation_changes_nothing(lab, monkeypatch):
    wid = crash_write(lab, monkeypatch, "outcome-before-commit")
    with restarted(lab):
        pass
    with restarted(lab) as h:  # reconciling a committed record again is a no-op
        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        assert ok(ws.call("new", {"title": "crash"}, opts={"idem": IDEM}))["duplicate"] is True
        assert len(tickets(lab)) == 1
        ws.close()
        assert h.host.services[wid].outcomes.reconcile_all() == []


def test_kill_between_two_appends_leaves_a_partial_record_after_restart(lab, monkeypatch):
    wid = crash_write(lab, monkeypatch, "outcome-after-first-append", op="apply")
    assert len(tickets(lab)) == 1  # event 1 of 2 landed
    (record,) = outcome_files(lab, wid)
    assert record["step"] == "pending" and len(record["planned"]) == 2
    with restarted(lab):
        (after,) = outcome_files(lab, wid)
        assert after["step"] == "partial" and after["seqs"] == [1]
        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        got = ok(ws.call("outcome.get", {"idem": IDEM}))["data"]
        assert got["state"] == "partial" and "interrupted after 1 event" in got["error"]["message"]
        retry = ws.call("apply", {"title": "crash"}, request_id="again", opts={"idem": IDEM})
        assert err(retry) == "invalid.input" and "interrupted after 1 event" in retry["error"]["message"]
        assert len(tickets(lab)) == 1  # the retry did not append the rest, or the first again
        ws.close()


def test_kill_between_two_appends_of_a_human_submit_stays_pending_for_the_roll_forward(lab, monkeypatch):
    wid = crash_write(lab, monkeypatch, "outcome-after-first-append", op="human.submit")
    with restarted(lab):
        (after,) = outcome_files(lab, wid)
        assert after["step"] == "pending"  # never partial: H5a rolls it forward
        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        assert ok(ws.call("outcome.get", {"idem": IDEM}))["data"] == {"state": "pending"}
        retry = ws.call("human.submit", {"title": "crash"}, opts={"idem": IDEM})
        assert err(retry) == "outcome.unknown" and retry["error"]["retryable"] is True
        assert len(tickets(lab)) == 1
        ws.close()


# --- H2a follow-up (a): _registered is exact ------------------------------------------------------------------------


def test_a_started_register_intent_is_not_credited_with_another_requests_workspace(lab):
    first = new_idem()
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c, idem=first))["data"]["workspace_id"]
    # a second register of the same root died before it recorded a workspace id (host dead, intent left started)
    second = new_idem()
    intents = IntentStore(lab.state, UID)
    intents.open()
    intents.begin(f"control:{UID}", f"uid:{UID}", second, "host.register", {"root": lab.root, "peers": lab.peers()})
    with lab.host() as h, h.client() as c:
        assert ("host.register", "failed") in h.host.recovered  # not committed with the first request's id
        outcome = ok(c.call("host.outcome", {"idem": second}))["data"]
        assert outcome["state"] == "failed" and outcome["error"]["code"] == "control.rolled_back"
        assert ok(c.call("host.outcome", {"idem": first}))["data"]["result"]["data"] == {"workspace_id": wid}
        assert [r["workspace_id"] for r in ok(c.call("host.list"))["data"]["workspaces"]] == [wid]


def test_a_started_register_intent_with_its_own_recorded_id_is_still_finished(lab):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
    idem = new_idem()
    intents = IntentStore(lab.state, UID)
    intents.open()
    intent = intents.begin(
        f"control:{UID}", f"uid:{UID}", idem, "host.register", {"root": lab.root, "peers": lab.peers()}
    )
    intents.update(intent, workspace_id=wid)  # the id it recorded before adding the entry
    with lab.host() as h, h.client() as c:
        assert ("host.register", "committed") in h.host.recovered
        assert ok(c.call("host.outcome", {"idem": idem}))["data"]["result"]["data"] == {"workspace_id": wid}


# --- H2a follow-up (b): a root a direct init took over is not cleaned -----------------------------------------------


def killed_bootstrap(lab, tmp_path):
    owner = Person()
    root = tmp_path / "fresh"
    root.mkdir()
    created = wsk_create(lab)
    idem = new_idem()
    crash(lab, "bootstrap-before-append", "host.bootstrap", bootstrap_args(created, root, owner), idem)
    return root, created, idem


def test_recovery_leaves_a_root_whose_config_names_another_workspace(lab, tmp_path):
    root, created, idem = killed_bootstrap(lab, tmp_path)
    foreign = {"workspace": {"id": "b" * 32, "prefix": "DIRECT"}}
    (root / ".orch" / "config.json").write_text(json.dumps(foreign))  # a direct creation ran here after the host died
    (root / ".orch" / "events").mkdir(exist_ok=True)
    (root / ".orch" / "events" / "workspace.jsonl").write_text("direct init genesis\n")
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.bootstrap", "failed")]
        outcome = ok(c.call("host.outcome", {"idem": idem}))["data"]
        assert outcome["state"] == "failed" and outcome["error"]["code"] == "control.rolled_back"
    assert json.loads((root / ".orch" / "config.json").read_text()) == foreign  # the other workspace's files are intact
    assert (root / ".orch" / "events" / "workspace.jsonl").read_text() == "direct init genesis\n"
    assert not (lab.state / "hosts" / created["workspace_id"]).exists()  # the host's own id and key still go


def test_recovery_still_cleans_a_root_whose_config_names_this_workspace(lab, tmp_path):
    root, created, _ = killed_bootstrap(lab, tmp_path)
    (root / ".orch" / "config.json").write_text(
        json.dumps({"workspace": {"id": created["workspace_id"]}})
    )  # the host's own
    with lab.host():
        pass
    assert os.listdir(root) == []  # the store's files go; the person's own directory stays


def test_recovery_leaves_a_root_whose_workspace_log_starts_with_another_genesis(lab, tmp_path):
    root, created, idem = killed_bootstrap(lab, tmp_path)
    # a direct creation that landed its genesis line but not yet its config.json
    for name in ("config.json",):
        (root / ".orch" / name).unlink(missing_ok=True)
    (root / ".orch" / "events").mkdir(exist_ok=True)
    line = json.dumps({"id": "01J9ZP0000000000000000DIRC", "type": "workspace.created", "seq": 1}) + "\n"
    (root / ".orch" / "events" / "workspace.jsonl").write_text(line)
    with lab.host() as h:
        assert h.host.recovered == [("host.bootstrap", "failed")]
    assert (root / ".orch" / "events" / "workspace.jsonl").read_text() == line  # not ours: left alone


def test_serving_a_workspace_twice_keeps_the_first_server_and_does_not_mark_it_unavailable(lab):
    """#1019: a ``host.register`` that lands while ``start()`` is still walking the registry is served by the request
    *and* by the start-up loop. The second ``serve`` used to fail on the socket lock (EAGAIN), replace the working
    service by an ``unavailable`` one, and answer every write ``workspace.unavailable``. A workspace is served once."""
    with restarted(lab) as h:
        with h.client() as c:
            wid = ok(lab.register(c))["data"]["workspace_id"]
        service, server = h.host.services[wid], h.host.servers[wid]
        h.run(h.host.serve(h.host.registry.entries()[0]))
        assert h.host.services[wid] is service and h.host.servers[wid] is server
        assert service.state == "ok", service.cause
        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        assert ok(ws.call("new", {"title": "twice"}, opts={"idem": IDEM}))["duplicate"] is False
        ws.close()
