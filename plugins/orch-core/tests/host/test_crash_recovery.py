"""H2a: kill -9 during register, unregister and bootstrap (a real second process), then recovery at host start.

The host runs in its own process (``crashhost.py``) and SIGKILLs itself at an exact step of the control write.
A new host on the same state directory finishes or rolls back the ``started`` intent; ``host.outcome`` and a
same-key retry return the stored terminal state.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from orch.host.control_client import ControlClient, new_idem
from tests.host.lifecycle_helpers import UID, Lab, genesis_for, host_process, wait_for_socket
from tests.identity.helpers import Person

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="Unix host")


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    finally:
        value.close()


def ok(res):
    assert res["t"] == "res" and res["ok"] is True, res
    return res["result"]


def err(res):
    assert res["t"] == "res" and res["ok"] is False, res
    return res["error"]["code"]


def crash(lab, point, op, args, idem):
    """Start a host process with ``point`` armed, send the control write, and see the process die by SIGKILL."""
    proc = host_process(lab.state, lab.run, crash=point, pins=lab.pins_dir)
    try:
        path = lab.run / "control" / "host.sock"
        wait_for_socket(path, proc)
        with ControlClient(path) as c:
            c.send(op, args, idem=idem)
            with pytest.raises((ConnectionError, OSError)):
                c.read(timeout=20)
        assert proc.wait(20) == -9
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def register_args(lab):
    return {"root": lab.root, "peers": lab.peers()}


def test_register_killed_before_the_entry_is_rolled_back(lab):
    idem = new_idem()
    crash(lab, "register-before-entry", "host.register", register_args(lab), idem)
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.register", "failed")]
        outcome = ok(c.call("host.outcome", {"idem": idem}))["data"]
        assert outcome["state"] == "failed" and outcome["exit"] == 9
        assert outcome["error"]["code"] == "control.rolled_back" and outcome["error"]["retryable"] is True
        res = c.call("host.register", register_args(lab), idem=idem)
        assert err(res) == "control.rolled_back" and res["exit"] == 9  # never run a second time
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
        ok(c.call("host.register", register_args(lab), idem=new_idem()))  # a new idem runs it


def test_register_killed_after_the_entry_is_committed_and_serves(lab):
    idem = new_idem()
    crash(lab, "register-after-entry", "host.register", register_args(lab), idem)
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.register", "committed")]
        outcome = ok(c.call("host.outcome", {"idem": idem}))["data"]
        wid = outcome["result"]["data"]["workspace_id"]
        assert outcome["state"] == "committed" and outcome["exit"] == 0
        retry = ok(c.call("host.register", register_args(lab), idem=idem))
        assert retry["data"] == {"workspace_id": wid} and retry["duplicate"] is True
        rows = ok(c.call("host.list"))["data"]["workspaces"]
        assert [(r["workspace_id"], r["state"]) for r in rows] == [(wid, "ok")]
    code, doc = lab.cli("status", run=True)  # the host is down: a host client says so, it never writes directly
    assert (code, doc["error"]["code"]) == (9, "host.unavailable")


@pytest.mark.parametrize("point", ["unregister-before-remove", "unregister-after-remove"])
def test_unregister_killed_is_finished_at_start(lab, point):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
    idem = new_idem()
    crash(lab, point, "host.unregister", {"workspace_id": wid}, idem)
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.unregister", "committed")]
        assert ok(c.call("host.outcome", {"idem": idem}))["data"] == {
            "state": "committed",
            "result": ok_result({}),
            "exit": 0,
        }
        assert ok(c.call("host.unregister", {"workspace_id": wid}, idem=idem))["duplicate"] is True
        assert ok(c.call("host.list"))["data"]["workspaces"] == []
    assert lab.cli("status")[0] == 0  # direct mode is back


def ok_result(data):
    from orch.host.workspace import ok_result as build

    return build(data)


def wsk_create(lab):
    proc = host_process(lab.state, lab.run, pins=lab.pins_dir)
    try:
        wait_for_socket(lab.run / "control" / "host.sock", proc)
        with ControlClient(lab.run / "control" / "host.sock") as c:
            created = ok(c.call("host.wsk_create"))["data"]
            ok(c.call("host.restart"))
        assert proc.wait(20) == 0  # a drained host exits; the service manager starts the next one
        return created
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def bootstrap_args(created, root, owner):
    return {
        "workspace_id": created["workspace_id"],
        "root": str(root),
        "events": [genesis_for(owner, created["workspace_id"], created["wsk_pub"])],
        "peers": {str(UID): {"class": "person", "person": owner.ref}},
    }


def test_bootstrap_killed_before_the_append_is_rolled_back_and_init_restarts_from_wsk_create(lab, tmp_path):
    owner = Person()
    root = tmp_path / "fresh"
    root.mkdir()
    created = wsk_create(lab)
    idem = new_idem()
    args = bootstrap_args(created, root, owner)
    crash(lab, "bootstrap-before-append", "host.bootstrap", args, idem)
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.bootstrap", "failed")]
        outcome = ok(c.call("host.outcome", {"idem": idem}))["data"]
        assert outcome["state"] == "failed" and outcome["error"]["code"] == "control.rolled_back"
        res = c.call("host.bootstrap", args, idem=idem)
        assert err(res) == "control.rolled_back"  # the same key never runs again
        # nothing is left behind: no workspace in the root (the person's own directory stays, empty), no key, no id
        assert os.listdir(root) == []
        assert not (lab.state / "hosts" / created["workspace_id"]).exists()
        stale = c.call("host.bootstrap", bootstrap_args(created, root, owner), idem=new_idem())
        assert err(stale) == "not_found"  # the id is gone: init starts again from wsk_create
        fresh = ok(c.call("host.wsk_create"))["data"]
        assert fresh["workspace_id"] != created["workspace_id"]
        result = ok(c.call("host.bootstrap", bootstrap_args(fresh, root, owner), idem=new_idem()))
        assert result["data"] == {"workspace_id": fresh["workspace_id"], "seq": 1}
        rows = ok(c.call("host.list"))["data"]["workspaces"]
        assert [(r["workspace_id"], r["state"], r["person"]) for r in rows] == [
            (fresh["workspace_id"], "ok", owner.ref)
        ]


@pytest.mark.parametrize("point", ["bootstrap-after-append", "bootstrap-after-register"])
def test_bootstrap_killed_after_the_append_is_finished_at_start(lab, tmp_path, point):
    owner = Person()
    root = tmp_path / "fresh"
    root.mkdir()
    created = wsk_create(lab)
    idem = new_idem()
    args = bootstrap_args(created, root, owner)
    crash(lab, point, "host.bootstrap", args, idem)
    with lab.host() as h, h.client() as c:
        assert h.host.recovered == [("host.bootstrap", "committed")]
        outcome = ok(c.call("host.outcome", {"idem": idem}))["data"]
        assert outcome["state"] == "committed"
        assert outcome["result"]["data"] == {"workspace_id": created["workspace_id"], "seq": 1}
        retry = ok(c.call("host.bootstrap", args, idem=idem))
        assert retry["duplicate"] is True and retry["seq"] == 1
        rows = ok(c.call("host.list"))["data"]["workspaces"]
        assert [(r["workspace_id"], r["state"]) for r in rows] == [(created["workspace_id"], "ok")]
        assert (lab.state / "hosts" / created["workspace_id"] / "genesis").is_file()
        assert not (lab.state / "hosts" / created["workspace_id"] / "wsk_create.json").exists()


def test_a_second_host_process_gets_host_busy(lab):
    first = host_process(lab.state, lab.run, pins=lab.pins_dir)
    try:
        wait_for_socket(lab.run / "control" / "host.sock", first)
        second = subprocess.run(
            [sys.executable, "-m", "orch.host", "run", "--state", str(lab.state), "--run", str(lab.short / "r2")],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ},
        )
        assert second.returncode == 4, second.stderr
        assert '"code": "host.busy"' in second.stdout and str(first.pid) in second.stdout
    finally:
        first.kill()
        first.wait()
        first.stdout.close()
        first.stderr.close()
