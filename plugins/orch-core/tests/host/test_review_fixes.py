"""PR #376 review round: each test fails without its fix (Opus security review 1-12, Sonnet review 1-2)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host.control_client import new_idem
from orch.host.intents import IntentStore
from orch.host.ownership import DirectGuard, OwnershipLock
from orch.host.pins import AgentPins
from orch.host.transport import Pin, handshake_pinned
from tests.host.lifecycle_helpers import UID, Lab, genesis_for, host_process, wait_for_socket
from tests.host.test_crash_recovery import crash
from tests.host.test_lifecycle import HELLO, err, ok
from tests.identity.helpers import Person
from tests.store.helpers import WS


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    finally:
        value.close()


def started_bootstrap(lab, workspace_id, root, *, validated=False):
    """A ``host.bootstrap`` intent as it is on disk after a SIGKILL right after ``begin`` (or after validation)."""
    store = IntentStore(lab.state, UID)
    store.open()
    args = {
        "workspace_id": workspace_id,
        "root": root,
        "events": [{"id": "01J0000000000000000000000Z", "type": "workspace.created"}],
        "peers": {str(UID): {"class": "person", "person": lab.owner}},
    }
    intent = store.begin(f"control:{UID}", f"uid:{UID}", new_idem(), "host.bootstrap", args)
    if validated:
        store.update(intent, validated=True)
    return intent


# --- 1 (high): recovery never deletes what this bootstrap did not create -------------------------------------


def test_an_unvalidated_bootstrap_intent_never_touches_an_existing_workspace(lab):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
    started_bootstrap(lab, wid, lab.root)  # the client's raw id and root, killed before any check
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.bootstrap", "failed")]
        assert ok(c.call("host.list"))["data"]["workspaces"][0]["state"] == "ok"
    assert (lab.ws.store_dir / "events").is_dir() and (lab.ws.store_dir / "config.json").is_file()
    assert (lab.state / "hosts" / wid / "keys").is_dir() and (lab.state / "hosts" / wid / "genesis").is_file()


def test_a_validated_bootstrap_intent_never_touches_a_registered_workspace_or_a_pinned_id(lab):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
    (lab.state / "hosts" / wid / "wsk_create.json").write_text("{}")  # even with a stray marker
    started_bootstrap(lab, wid, lab.root, validated=True)
    with lab.host() as h:
        assert h.host.recovered == [("host.bootstrap", "failed")]
    assert (lab.ws.store_dir / "events").is_dir() and (lab.state / "hosts" / wid / "keys").is_dir()


def test_bootstrap_refusals_before_validation_store_nothing(lab, tmp_path):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
        args = {
            "workspace_id": wid,
            "root": lab.root,
            "events": [{"id": "x", "type": "workspace.created"}],
            "peers": lab.peers(),
        }
        idem = new_idem()
        assert err(c.call("host.bootstrap", args, idem=idem)) == "host.busy"
        assert err(c.call("host.outcome", {"idem": idem})) == "outcome.unknown_idem"
    intents = lab.state / "host" / "intents"
    assert [json.loads((intents / n).read_text())["op"] for n in os.listdir(intents)] == ["host.register"]


# --- 8: a forged intent cannot reach rmtree outside the state directory --------------------------------------


def test_a_forged_intent_with_path_traversal_is_failed_untouched(lab, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    rel = os.path.relpath(victim, lab.state / "hosts")
    started_bootstrap(lab, rel, str(tmp_path / "nonexistent-root"), validated=True)
    with lab.host() as h:
        assert h.host.recovered == [("host.bootstrap", "failed")]
    assert (victim / "keep.txt").exists()


# --- 2 and 3: a landed genesis is only ever finished -----------------------------------------------------------


def wsk_and_args(c, root, owner):
    created = ok(c.call("host.wsk_create"))["data"]
    args = {
        "workspace_id": created["workspace_id"],
        "root": os.path.realpath(root),
        "events": [genesis_for(owner, created["workspace_id"], created["wsk_pub"])],
        "peers": {str(UID): {"class": "person", "person": owner.ref}},
    }
    return created["workspace_id"], args


def test_lock_busy_after_the_append_keeps_the_intent_and_a_retry_finishes_it(lab, tmp_path):
    owner = Person()
    root = tmp_path / "newroot"
    root.mkdir(mode=0o700)
    with lab.host(lock_wait=0.3) as h, h.client() as c:
        wid, args = wsk_and_args(c, root, owner)
        idem = new_idem()
        with OwnershipLock(lab.state, wid).shared(5) as held:  # a direct CLI in the new root, say
            assert held
            # the genesis landed: never a refusal (H3b3 F1), the client asks again with the same idem
            assert err(c.call("host.bootstrap", args, idem=idem)) == "outcome.unknown"
        assert (root / ".orch" / "events" / "workspace.jsonl").is_file()  # the genesis landed and stays
        assert ok(c.call("host.outcome", {"idem": idem}))["data"] == {"state": "pending"}
        result = ok(c.call("host.bootstrap", args, idem=idem))  # the same key finishes it
        assert result["data"] == {"workspace_id": wid, "seq": 1}
        assert ok(c.call("host.outcome", {"idem": idem}))["data"]["state"] == "committed"
        assert [r["workspace_id"] for r in ok(c.call("host.list"))["data"]["workspaces"]] == [wid]


def test_a_landed_genesis_that_cannot_be_finished_at_start_stays_started_and_is_finished_later(lab, tmp_path):
    owner = Person()
    root = tmp_path / "newroot"
    root.mkdir(mode=0o700)
    proc = host_process(lab.state, lab.run, pins=lab.pins_dir)
    try:
        wait_for_socket(lab.run / "control" / "host.sock", proc)
        from orch.host.control_client import ControlClient

        with ControlClient(lab.run / "control" / "host.sock") as c:
            wid, args = wsk_and_args(c, root, owner)
            ok(c.call("host.restart"))
        proc.wait(20)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.stderr.close()
    idem = new_idem()
    crash(lab, "bootstrap-after-append", "host.bootstrap", args, idem)
    with OwnershipLock(lab.state, wid).shared(5) as held:  # the registration cannot take the lock at start
        assert held
        with lab.host(lock_wait=0.2) as h, h.client() as c:
            assert h.host.recovered == [("host.bootstrap", "started")]
            assert ok(c.call("host.outcome", {"idem": idem}))["data"] == {"state": "pending"}
    assert (root / ".orch" / "events" / "workspace.jsonl").is_file()
    assert (lab.state / "hosts" / wid / "keys").is_dir()
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.bootstrap", "committed")]
        assert [r["workspace_id"] for r in ok(c.call("host.list"))["data"]["workspaces"]] == [wid]


# --- 4: D-P2-17 file custody at register and at serve --------------------------------------------------------


def key_file(lab):
    keys = lab.state / "hosts" / WS / "keys"
    return next(p for p in keys.iterdir() if p.is_file())


@pytest.mark.parametrize(
    "loosen",
    [
        lambda lab: os.chmod(key_file(lab), 0o644),
        lambda lab: os.chmod(lab.state / "hosts" / WS / "keys", 0o755),
        lambda lab: os.chmod(lab.state / "hosts" / WS / "genesis", 0o666),
        lambda lab: os.chmod(lab.ws.store_dir / "events", 0o777),
        lambda lab: os.chmod(lab.ws.store_dir / ".state", 0o777),
        lambda lab: os.chmod(lab.ws.store_dir / "tickets", 0o777),
    ],
    ids=["wsk-0644", "keys-0755", "pin-0666", "events-0777", "state-0777", "tickets-0777"],
)
def test_register_refuses_loose_custody(lab, loosen):
    loosen(lab)
    with lab.host() as h, h.client() as c:
        res = lab.register(c)
        assert err(res) == "custody.unsafe_mode", res


def test_a_served_workspace_with_loose_custody_is_unavailable_at_start(lab):
    with lab.host() as h, h.client() as c:
        ok(lab.register(c))
    os.chmod(key_file(lab), 0o644)
    with lab.host() as h, h.client() as c:
        assert ok(c.call("host.list"))["data"]["workspaces"][0]["state"] == "unavailable"
        assert "custody" in h.host.services[WS].cause


# --- 5: pins are rechecked per request and fail closed --------------------------------------------------------


def test_a_uid_pinned_after_registration_reads_as_no_person(lab):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
        lab.pin_agent(UID, workspaces=[wid])  # H3c pins this UID later
        assert ok(c.call("host.list"))["data"]["workspaces"][0]["person"] is None
        sock, welcome = handshake_pinned(Pin(UID, wid, lab.run / wid / "host.sock"), HELLO)
        sock.close()
        assert welcome["peer"]["person"] is None


def test_an_unreadable_pin_directory_fails_closed(lab):
    lab.pin_agent(UID)
    os.chmod(lab.pins_dir, 0o000)
    try:
        assert AgentPins(lab.pins_dir, owner=UID).pinned(UID)
        with lab.host() as h, h.client() as c:
            assert err(lab.register(c)) == "op.not_allowed"
    finally:
        os.chmod(lab.pins_dir, 0o755)


# --- 6: orch host register --------------------------------------------------------------------------------------


def test_orch_host_register_waits_past_the_lock_and_prints_its_idem(lab, monkeypatch):
    from orch.cli.main import main
    from orch.host import control_client

    seen = {}
    real = control_client.ControlClient

    def spy(path, **kw):
        seen.update(kw, path=path)
        return real(path, **kw)

    monkeypatch.setattr(control_client, "ControlClient", spy)
    argv = ["host", "register", lab.root, "--peer", f"{UID}=person:{lab.owner}", "--run", str(lab.run), "--no-service"]
    env = {"ORCH_STATE_DIR": str(lab.state), "HOME": str(lab.tmp)}
    out = io.StringIO()
    assert main(argv, env=env, stdout=out) == 9  # no host yet
    doc = json.loads(out.getvalue())
    assert doc["error"]["code"] == "host.unavailable" and doc["idem"].startswith("a_")
    with lab.host():
        out = io.StringIO()
        assert main(argv, env=env, stdout=out) == 0
        doc = json.loads(out.getvalue())
        assert doc["ok"] is True and doc["idem"].startswith("a_")
        assert seen["timeout"] > 60
        out = io.StringIO()
        assert main(["host", "outcome", doc["idem"], "--run", str(lab.run)], env=env, stdout=out) == 0
        assert json.loads(out.getvalue())["result"]["data"]["state"] == "committed"


def test_a_planted_tier_b_run_directory_never_wins_over_the_tier_a_one(monkeypatch, tmp_path):
    """H7 review: any local account can create /Users/Shared/orch/run/<uid>; in tier A only --run, ORCH_HOST_RUN_DIR
    or <state>/run is used, by orch host, the thin CLI and orch sign alike."""
    from orch import hostclient
    from orch.host import cli, handoff, listener

    service = tmp_path / "svc"
    monkeypatch.setattr(listener, "default_run_directory", lambda uid: service)
    wid = "0" * 32
    for d in ("control", wid):
        (service / d).mkdir(parents=True)
        (service / d / "host.sock").touch()
    state = tmp_path / "state"
    assert cli._control_path(None, state, {}) == state / "run" / "control" / "host.sock"
    assert cli._control_path(tmp_path / "r", state, {}) == tmp_path / "r" / "control" / "host.sock"
    assert cli._control_path(None, state, {"ORCH_HOST_RUN_DIR": "/x"}) == Path("/x/control/host.sock")
    assert hostclient.workspace_socket({}, state, UID, wid) == state / "run" / wid / "host.sock"
    assert handoff._socket(None, state, UID, wid) == state / "run" / wid / "host.sock"


# --- 7: drain flushes queued answers --------------------------------------------------------------------------


def test_drain_waits_for_queued_answers(lab):
    from orch.host.service import Host

    host = Host(lab.state, lab.run)
    session = SimpleNamespace(closed=False, answering={"r"}, queue=asyncio.Queue(), writing=False)
    host.servers["x"] = (SimpleNamespace(sessions=[session]), None)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, session.answering.clear)
        started = loop.time()
        await host._flush()
        return loop.time() - started

    assert asyncio.run(scenario()) >= 0.3


# --- 10, 11: hosts/ is private; roots never inside the host's own directories ----------------------------------


def test_the_direct_guard_creates_hosts_private_whatever_the_umask(tmp_path):
    old = os.umask(0o002)
    try:
        state = tmp_path / "fresh"
        state.mkdir(mode=0o700)
        with DirectGuard(state, "0" * 32, pins=AgentPins(tmp_path / "nopins", owner=UID))():
            pass
    finally:
        os.umask(old)
    assert stat.S_IMODE((state / "hosts").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "hosts" / ("0" * 32)).stat().st_mode) == 0o700


def test_roots_inside_the_state_directory_are_refused(lab):
    owner = Person()
    with lab.host() as h, h.client() as c:
        created = ok(c.call("host.wsk_create"))["data"]
        wid = created["workspace_id"]
        inner = lab.state / "hosts" / wid / "inner"
        inner.mkdir(mode=0o700)
        args = {
            "workspace_id": wid,
            "root": str(inner),
            "events": [genesis_for(owner, wid, created["wsk_pub"])],
            "peers": {str(UID): {"class": "person", "person": owner.ref}},
        }
        assert err(c.call("host.bootstrap", args, idem=new_idem())) == "invalid.input"
        assert os.listdir(inner) == []


# --- 12: release and units ------------------------------------------------------------------------------------


def test_release_current_link_rehash_and_no_root_unit(tmp_path):
    from orch.host import units
    from orch.host.release import ReleaseError, install_release

    source = Path(__import__("orch").__file__).parent
    target = tmp_path / "releases"
    release = install_release("1.0.0", target=target, source=source)
    assert os.readlink(target / "current") == "1.0.0"
    victim = release / "orch" / "host" / "units.py"
    os.chmod(victim, 0o644)
    victim.write_text("# tampered\n")
    with pytest.raises(ReleaseError):
        install_release("1.0.0", target=target, source=source)
    for path in [release, *release.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    with pytest.raises(ValueError, match="root"):
        units.systemd_unit(Path("/py"), Path("/r"), Path("/s"), system=True, user="root", host_uid=0)


# --- Sonnet 2: the direct-mode guard does not load the transport ----------------------------------------------


def test_the_direct_mode_guard_loads_no_transport():
    code = "import json, sys, orch.host.ownership; print(json.dumps([m for m in sys.modules if m.startswith('orch')]))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True).stdout
    loaded = set(json.loads(out))
    assert not loaded & {"orch.host.transport", "orch.host.frames", "orch.host.listener", "orch.host.client"}
