"""H2a acceptance: registry, ownership lock and adoption, control ops, intents, states, drain (real sockets)."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from orch.host.control_client import ControlClient, new_idem
from orch.host.errors import HostError
from orch.host.ownership import LOCK_WAIT_SECONDS, DirectGuard, OwnershipLock
from orch.host.registry import registry_path
from orch.host.service import Host
from orch.host.transport import Pin, handshake_pinned
from orch.store import StoreError
from tests.host.lifecycle_helpers import UID, Lab, genesis_for
from tests.identity.helpers import Person

HELLO = {"t": "hello", "proto": [1], "client": "test", "caps": []}


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
    assert res["t"] == "res" and res["ok"] is True, res
    return res["result"]


def err(res):
    assert res["t"] == "res" and res["ok"] is False, res
    return res["error"]["code"]


# --- registry, list and the caller's mapping -------------------------------------------------------------------


def test_register_persists_a_private_registry_and_list_shows_the_callers_mapping(lab):
    with lab.host() as h, h.client() as c:
        assert ok(c.call("host.list"))["data"] == {"workspaces": []}
        result = ok(lab.register(c))
        wid = result["data"]["workspace_id"]
        rows = ok(c.call("host.list"))["data"]["workspaces"]
    assert rows == [
        {
            "workspace_id": wid,
            "prefix": "DEMO",
            "root": lab.root,
            "state": "ok",
            "tier": "A",
            "socket": str(lab.run / wid / "host.sock"),
            "person": lab.owner,  # R11: the caller UID's mapping
        }
    ]
    path = registry_path(lab.state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    doc = json.loads(path.read_text())
    assert doc["workspaces"][0]["peers"] == {str(UID): {"class": "person", "person": lab.owner}}
    assert doc["workspaces"][0]["group"] == f"orch-{wid[:12]}"


def test_list_shows_no_person_for_an_unmapped_caller(lab, tmp_path):
    other = 4_000_000_000
    with lab.host() as h, h.client() as c:
        ok(lab.register(c, peers={str(other): {"class": "person", "person": lab.owner}}))
        assert ok(c.call("host.list"))["data"]["workspaces"][0]["person"] is None


def test_a_removed_members_uid_reads_as_no_person(lab):
    quinn = lab.add_member()
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c, peers=lab.peers(quinn.ref)))["data"]["workspace_id"]
        assert ok(c.call("host.list"))["data"]["workspaces"][0]["person"] == quinn.ref
        sock, welcome = handshake_pinned(Pin(UID, wid, lab.run / wid / "host.sock"), HELLO)
        sock.close()
        assert welcome["peer"] == {"uid": UID, "class": "person", "person": quinn.ref}
        lab.remove_member(quinn)  # F8: the mapping is checked again on every read
        assert ok(c.call("host.list"))["data"]["workspaces"][0]["person"] is None
        sock, welcome = handshake_pinned(Pin(UID, wid, lab.run / wid / "host.sock"), HELLO)
        sock.close()
        assert welcome["peer"] == {"uid": UID, "class": "person", "person": None}


def test_register_refuses_a_pinned_agent_uid_as_person(lab):
    lab.pin_agent(UID)
    with lab.host() as h, h.client() as c:
        res = lab.register(c)
        assert err(res) == "op.not_allowed" and "pin" in res["error"]["message"]
        assert ok(c.call("host.list"))["data"]["workspaces"] == []


def test_a_pin_that_others_can_write_fails_closed(lab):
    lab.pin_agent(UID, mode=0o666)  # tampered with, or could be: still a pin, never a person
    with lab.host() as h, h.client() as c:
        assert err(lab.register(c)) == "op.not_allowed"


def test_register_refuses_a_person_who_is_not_a_member_with_a_decide_device(lab):
    outsider = Person()
    revoked = lab.add_member()
    lab.revoke_device(revoked)
    with lab.host() as h, h.client() as c:
        assert err(lab.register(c, peers=lab.peers(outsider.ref))) == "op.not_allowed"
        assert err(lab.register(c, peers=lab.peers(revoked.ref))) == "op.not_allowed"


def test_register_peer_rules(lab):
    with lab.host() as h, h.client() as c:
        assert err(lab.register(c, peers={str(UID): {"class": "agent"}})) == "op.not_allowed"
        assert err(lab.register(c, peers={str(UID): {"class": "worker"}})) == "op.not_allowed"
        assert err(lab.register(c, peers={str(UID): {"class": "person"}})) == "invalid.input"
        assert err(lab.register(c, peers={"x": {"class": "agent"}})) == "invalid.input"
        both = {**lab.peers(), "70001": {"class": "worker"}, "70002": {"class": "worker"}}
        assert err(lab.register(c, peers=both)) == "op.not_allowed"


def test_register_refuses_unsafe_roots_and_duplicates(lab, tmp_path):
    with lab.host() as h, h.client() as c:
        os.chmod(lab.root, 0o777)
        assert err(lab.register(c)) == "custody.unsafe_mode"
        os.chmod(lab.root, 0o755)
        assert err(lab.register(c, root=str(tmp_path / "nope"))) == "not_found"
        (tmp_path / "empty").mkdir()
        assert err(lab.register(c, root=str(tmp_path / "empty"))) == "invalid.input"
        link = tmp_path / "link"
        link.symlink_to(lab.root)
        ok(lab.register(c, root=str(link)))  # canonicalised with realpath: the same root
        assert err(lab.register(c)) == "host.busy"


def test_register_refuses_a_workspace_without_or_with_another_genesis_pin(lab):
    pin = lab.state / "hosts" / lab.ws.root.name  # not the pin; the real one is under the workspace id
    from tests.store.helpers import WS

    pin = lab.state / "hosts" / WS / "genesis"
    original = pin.read_bytes()
    pin.unlink()
    with lab.host() as h, h.client() as c:
        assert err(lab.register(c)) == "trust.genesis_mismatch"
        pin.write_bytes(b"sha256:" + b"0" * 64 + b"\n")
        assert err(lab.register(c)) == "trust.genesis_mismatch"
        pin.write_bytes(original)
        ok(lab.register(c))


# --- adoption: the ownership lock and the direct CLI -----------------------------------------------------------


def test_the_cli_is_a_host_client_after_adoption_and_unregister_restores_direct_mode(lab):
    code, doc = lab.cli("status")
    assert code == 0, doc
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
        code, doc = lab.cli("status", run=True)  # registered and served: a request to the host (H4b)
        assert code == 0, doc
        code, doc = lab.cli("list", run=True)
        assert code == 0, doc
        ok(c.call("host.unregister", {"workspace_id": wid}, idem=new_idem()))
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
        assert not (lab.run / wid / "host.sock").exists()
    code, doc = lab.cli("status")
    assert code == 0, doc


def test_a_registered_workspace_with_the_host_down_is_host_unavailable_never_direct(lab):
    with lab.host() as h, h.client() as c:
        ok(lab.register(c))
    code, doc = lab.cli("status", run=True)  # never a silent fallback to direct mode (D-P2-18)
    assert (code, doc["error"]["code"]) == (9, "host.unavailable")


def test_direct_guard_waits_for_adoption_then_answers_host_required(lab):
    from tests.store.helpers import WS

    guard = DirectGuard(lab.state, WS, timeout=0.2, pins=lab.pins)
    with guard():  # unregistered: the transaction runs under the shared lock
        with guard():  # re-entrant in one thread
            assert OwnershipLock(lab.state, WS).holders() == [os.getpid()]
    with OwnershipLock(lab.state, WS).exclusive(1):
        with pytest.raises(StoreError) as caught, guard():
            pass
        assert caught.value.code == "store.busy"
    lab.pin_agent(UID, [WS])  # an agent UID's pin names the workspace: registered for it
    with pytest.raises(Exception) as caught, guard():
        pass
    assert getattr(caught.value, "code", None) == "host.required"


def test_register_gives_up_on_the_ownership_lock_with_lock_busy_naming_the_holders(lab):
    from tests.store.helpers import WS

    assert LOCK_WAIT_SECONDS == 60 and Host(lab.state, lab.run).lock_wait == 60
    with lab.host(lock_wait=0.3) as h, h.client() as c:
        with OwnershipLock(lab.state, WS).shared(1) as held:  # a direct-mode transaction in progress
            assert held
            started = time.monotonic()
            idem = new_idem()
            res = lab.register(c, idem)
            assert time.monotonic() - started >= 0.3
        assert err(res) == "lock.busy"
        assert res["exit"] == 9 and res["error"]["retryable"] is True and res["error"]["retry_after_ms"] > 0
        assert str(os.getpid()) in res["error"]["message"]
        ok(lab.register(c, idem))  # lock.busy stored nothing: the same key runs again once the lock is free


# --- control ops: idempotency, outcome, wsk_create -------------------------------------------------------------


def test_control_writes_need_an_idem_and_replay_by_key(lab):
    with lab.host() as h, h.client() as c:
        assert err(c.call("host.register", {"root": lab.root, "peers": lab.peers()})) == "idem.required"
        idem = new_idem()
        first = ok(lab.register(c, idem))
        again = ok(lab.register(c, idem))
        assert again == {**first, "duplicate": True} and first["duplicate"] is False
        assert err(lab.register(c, idem, peers={"4000000000": {"class": "agent"}})) == "idem.conflict"
        outcome = ok(c.call("host.outcome", {"idem": idem}))["data"]
        assert outcome == {"state": "committed", "result": first, "exit": 0}
        assert err(c.call("host.outcome", {"idem": new_idem()})) == "outcome.unknown_idem"
        refused = new_idem()
        assert err(lab.register(c, refused)) == "host.busy"
        assert ok(c.call("host.outcome", {"idem": refused}))["data"]["state"] == "failed"
        assert err(lab.register(c, refused)) == "host.busy"  # a stored failure, never a second run


def test_control_envelope_rules(lab):
    with lab.host() as h, h.client() as c:
        assert err(c.call("host.list", {}, auth={"session": "x"})) == "req.invalid"
        assert err(c.call("host.list", {"x": 1})) == "invalid.input"
        assert err(c.call("host.list", {}, opts={"dry_run": True})) == "invalid.input"
        assert err(c.call("status")) == "unknown_command"
        assert c.welcome["workspace"] is None and c.welcome["peer"]["class"] == "control"
        assert set(c.welcome["ops"]) >= {"host.list", "host.register", "host.outcome", "host.restart"}


def test_wsk_create_never_replaces_a_key(lab, monkeypatch):
    with lab.host() as h, h.client() as c:
        first = ok(c.call("host.wsk_create"))["data"]
        wid = first["workspace_id"]
        key = lab.state / "hosts" / wid / "keys" / os.listdir(lab.state / "hosts" / wid / "keys")[0]
        before = key.read_bytes()
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(key.parent.parent.stat().st_mode) == 0o700
        ids = iter([wid, wid, "f" * 32])
        monkeypatch.setattr(h.host, "new_workspace_id", lambda: next(ids))
        second = ok(c.call("host.wsk_create"))["data"]
        assert second["workspace_id"] == "f" * 32 and second["wsk_pub"] != first["wsk_pub"]
        assert key.read_bytes() == before
        monkeypatch.setattr(h.host, "new_workspace_id", lambda: wid)
        assert err(c.call("host.wsk_create")) == "internal"
        assert key.read_bytes() == before
    from orch.custody import FileBackend, KeyExists

    with pytest.raises(KeyExists):
        FileBackend(key.parent).create("wsk")


def test_an_unused_wsk_create_key_is_swept_after_an_hour(lab):
    now = [time.time()]
    with lab.host(clock=lambda: now[0]) as h, h.client() as c:
        wid = ok(c.call("host.wsk_create"))["data"]["workspace_id"]
        now[0] += 3601
        ok(c.call("host.wsk_create"))
    assert not (lab.state / "hosts" / wid).exists()


# --- bootstrap ------------------------------------------------------------------------------------------------


def bootstrap_args(lab, created, root, owner):
    return {
        "workspace_id": created["workspace_id"],
        "root": str(root),
        "events": [genesis_for(owner, created["workspace_id"], created["wsk_pub"])],
        "peers": {str(UID): {"class": "person", "person": owner.ref}},
    }


def test_bootstrap_appends_the_genesis_and_registers(lab, tmp_path):
    owner = Person()
    root = tmp_path / "fresh"
    root.mkdir()
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        idem = new_idem()
        result = ok(c.call("host.bootstrap", bootstrap_args(lab, created, root, owner), idem=idem))
        assert result["data"] == {"workspace_id": created["workspace_id"], "seq": 1} and result["seq"] == 1
        rows = ok(c.call("host.list"))["data"]["workspaces"]
        assert [(r["workspace_id"], r["prefix"], r["state"], r["person"]) for r in rows] == [
            (created["workspace_id"], "BOOT", "ok", owner.ref)
        ]
        assert (lab.state / "hosts" / created["workspace_id"] / "genesis").is_file()
        again = bootstrap_args(lab, created, root, owner)
        assert err(c.call("host.bootstrap", again, idem=new_idem())) == "host.busy"  # N5


def test_bootstrap_refusals(lab, tmp_path):
    owner = Person()
    root = tmp_path / "fresh"
    root.mkdir()
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        args = bootstrap_args(lab, created, root, owner)
        assert err(c.call("host.bootstrap", {**args, "workspace_id": "e" * 32}, idem=new_idem())) == "not_found"
        (root / ".orch").write_text("x")  # a project's own .orch
        assert err(c.call("host.bootstrap", args, idem=new_idem())) == "invalid.input"
        (root / ".orch").unlink()
        other = Person()
        bad_peer = {**args, "peers": {str(UID): {"class": "person", "person": other.ref}}}
        assert err(c.call("host.bootstrap", bad_peer, idem=new_idem())) == "op.not_allowed"
        assert err(c.call("host.bootstrap", args, idem=new_idem(), auth={"grant": "gr_x"})) == "req.invalid"
        assert os.listdir(root) == []  # nothing was left behind; the id is still usable
        ok(c.call("host.bootstrap", args, idem=new_idem()))


# --- workspace states and the effective column on the socket -----------------------------------------------


class Backend:
    caps = ["ops/1"]
    ops = {"t.read": 1, "t.write": 1, "t.agent": 1, "t.wait": 1}

    def __init__(self):
        self.release = threading.Event()
        self.writing = threading.Event()
        self.parked = threading.Event()

    def kind(self, op):
        return {
            "t.read": ("read", "read"),
            "t.write": ("unattended", "write"),
            "t.agent": ("agent", "write"),
            "t.wait": ("read", "wait"),
        }.get(op)

    async def run(self, call):
        from orch.host.workspace import ok_result

        op = call.frame["op"]
        if op == "t.write":
            self.writing.set()
            await asyncio.to_thread(self.release.wait, 10)
        if op == "t.wait":
            self.parked.set()
            await asyncio.sleep(60)
        return ok_result({"op": op, "column": call.column.name, "viewer": call.viewer})


class WsClient:
    def __init__(self, path, wid):
        from orch.host import frames

        self.frames = frames
        self.sock, self.welcome = handshake_pinned(Pin(UID, wid, path), HELLO)
        self.decoder = frames.Decoder()
        self.pending = []

    def send(self, request_id, op, **extra):
        self.sock.sendall(self.frames.encode({"t": "req", "id": request_id, "op": op, "args": {}, **extra}))

    def read(self, timeout=5):
        from orch.host._socket import recv

        self.sock.settimeout(timeout)
        while not self.pending:
            data = recv(self.sock)
            assert data, "connection closed"
            self.pending.extend(self.decoder.feed(data))
        return self.pending.pop(0)

    def call(self, op, request_id="r", **extra):
        self.send(request_id, op, **extra)
        return self.read()

    def close(self):
        self.sock.close()


def serve_lab(lab, backend, peers=None, **kw):
    h = lab.host(backend=lambda: backend, **kw)
    h.__enter__()
    c = h.client()
    wid = ok(lab.register(c, peers=peers))["data"]["workspace_id"]
    return h, c, wid


def test_degraded_refuses_writes_and_unavailable_refuses_all(lab):
    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend)
    try:
        ws = WsClient(lab.run / wid / "host.sock", wid)
        assert ok(ws.call("t.read"))["data"]["column"] == "P"
        service = h.host.services[wid]

        def force(state, cause):  # what _judge concludes from the store (unit-tested below)
            service._judge = lambda: (setattr(service, "state", state), setattr(service, "cause", cause))

        force("degraded", "an unacknowledged invalid event")
        assert ok(ws.call("t.read"))["data"]["op"] == "t.read"
        res = ws.call("t.write")
        assert err(res) == "workspace.degraded" and res["exit"] == 3
        force("unavailable", "chain broken at genesis")
        for op in ("t.read", "t.write", "t.wait"):
            res = ws.call(op)
            assert err(res) == "workspace.unavailable" and "chain broken" in res["error"]["message"]
        ws.close()
    finally:
        c.close()
        h.stop()


def test_unavailable_at_start_names_the_cause(lab):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
    os.chmod(lab.root, 0o777)
    try:
        with lab.host() as h, h.client() as c:
            row = ok(c.call("host.list"))["data"]["workspaces"][0]
            assert row["state"] == "unavailable"
            ws = WsClient(lab.run / wid / "host.sock", wid)
            res = ws.call("host.status")
            assert err(res) == "workspace.unavailable" and "writable" in res["error"]["message"]
            ws.close()
    finally:
        os.chmod(lab.root, 0o755)


def test_a_degraded_judgement_comes_from_unacknowledged_invalid_events(lab):
    from types import SimpleNamespace

    from orch.host.registry import Entry
    from orch.host.workspace import WorkspaceService

    service = WorkspaceService(Entry("a" * 32, "X", "/x", UID, "g", {}), lab.state, UID)
    item = SimpleNamespace(acknowledged=False)
    view = SimpleNamespace(genesis="sha256:x", workspace_id="a" * 32, frozen=False, invalid=(item,))
    service.store = SimpleNamespace(state=SimpleNamespace(workspace=view), close=lambda: None)
    service._judge()
    assert service.state == "degraded"
    item.acknowledged = True
    service._judge()
    assert service.state == "ok"
    view.genesis = None
    service._judge()
    assert service.state == "unavailable"
    service.executor.shutdown()


def test_invalid_mapping_refuses_every_write_and_unverified_grant_gives_grant_expired(lab):
    quinn = lab.add_member()
    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend, peers=lab.peers(quinn.ref))
    try:
        ws = WsClient(lab.run / wid / "host.sock", wid)
        assert ok(ws.call("t.read"))["data"] == {"op": "t.read", "column": "P", "viewer": quinn.ref}
        assert ok(ws.call("t.write"))["data"]["column"] == "P"
        assert err(ws.call("t.agent")) == "op.not_allowed"  # P has no agent ops
        unknown = "gr_01J9ZP0000000000000000000Z." + "A" * 43
        assert err(ws.call("t.agent", auth={"grant": unknown})) == "grant.expired"  # not op.not_allowed
        assert err(ws.call("t.agent", auth={"grant": "nope"})) == "grant.required"
        assert ok(ws.call("t.read", auth={"grant": unknown}))["data"]["column"] == "N"
        lab.remove_member(quinn)
        res = ws.call("t.read")
        assert ok(res)["data"] == {"op": "t.read", "column": "N", "viewer": None}
        assert err(ws.call("t.write")) == "op.not_allowed"  # L1
        assert err(ws.call("t.agent")) == "op.not_allowed"
        ws.close()
    finally:
        c.close()
        h.stop()


def test_a_verified_grant_runs_in_column_g(lab):
    backend = Backend()
    backend.release.set()
    h, c, wid = serve_lab(lab, backend, clock=lambda: lab.ws.clock[0])  # the grant lives 8 h on the test clock
    try:
        ws = WsClient(lab.run / wid / "host.sock", wid)
        res = ok(ws.call("t.agent", auth={"grant": lab.ws.grant}))
        assert res["data"] == {"op": "t.agent", "column": "G", "viewer": lab.owner}
        ws.close()
    finally:
        c.close()
        h.stop()


# --- drain and restart ---------------------------------------------------------------------------------------


def test_drain_finishes_a_write_in_flight_and_ends_wait_with_host_restarting(lab):
    backend = Backend()
    h, c, wid = serve_lab(lab, backend)
    try:
        ws = WsClient(lab.run / wid / "host.sock", wid)
        ws.send("w", "t.write")
        ws.send("p", "t.wait")
        assert backend.writing.wait(5)
        assert backend.parked.wait(5)  # the wait is parked before the drain starts
        assert ok(c.call("host.restart"))["data"] == {"draining": True}
        parked = ws.read()
        assert parked["id"] == "p" and err(parked) == "host.restarting"
        assert parked["error"]["retryable"] is True and parked["error"]["retry_after_ms"] > 0
        late = ws.call("t.read", request_id="n")
        assert late["id"] == "n" and err(late) == "host.restarting"
        assert err(c.call("host.list")) == "host.restarting"
        assert h.thread.is_alive()  # the write still runs: the host waits for it
        backend.release.set()
        done = ws.read()
        assert done["id"] == "w" and ok(done)["data"]["op"] == "t.write"
        h.thread.join(10)
        assert not h.thread.is_alive()
        ws.close()
    finally:
        backend.release.set()
        c.close()
        h.stop()


# --- second host ---------------------------------------------------------------------------------------------


def test_a_second_host_gets_host_busy(lab):
    with lab.host():
        with pytest.raises(HostError) as caught:
            asyncio.run(Host(lab.state, lab.short / "r2", pins=lab.pins).start())
        assert caught.value.code == "host.busy" and str(os.getpid()) in caught.value.message


def test_host_refuses_an_unsafe_state_directory(lab):
    os.chmod(lab.state, 0o777)
    try:
        with pytest.raises(HostError) as caught, lab.host():
            pass
        assert caught.value.code == "custody.unsafe_mode"
    finally:
        os.chmod(lab.state, 0o700)


def test_a_client_connects_only_to_the_trusted_control_socket(lab):
    with lab.host() as h:
        with pytest.raises(Exception):  # noqa: B017 - the pin names another host UID
            ControlClient(h.host.control_path, host_uid=UID + 1)


def test_register_works_in_a_project_that_already_has_files(lab):
    """Plain `orch setup` in an existing project, then `orch host register .`: the project's own files do not matter."""
    (Path(lab.root) / "README.md").write_text("project")
    (Path(lab.root) / "src").mkdir()
    (Path(lab.root) / "src" / "a.py").write_text("x")
    with lab.host() as h, h.client() as c:
        ok(lab.register(c, new_idem()))
    assert (Path(lab.root) / "README.md").read_text() == "project"
