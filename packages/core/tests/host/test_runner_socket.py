"""H11a2: the runner socket, the command stream, launch intent and reconciliation, the terminal socket, run.list and
run.sub.

The runner runs in this test process (supervisor, host link and terminal socket on threads), so the main-PID check is
a fake that accepts this process's PID. Every process started here is a run of that supervisor, stopped in teardown.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orch.host import frames
from orch.host.client import Pin, handshake_pinned
from orch.host.errors import HostError
from orch.host.runners import main_pid, new_run_id
from orch.runner import enablement, procs
from orch.runner.config import RunnerConfig
from orch.runner.grants import GrantStore
from orch.runner.link import HostLink, build_env
from orch.runner.supervise import SpawnError, Supervisor
from orch.runner.term import TermListener
from tests.host.dispatch_helpers import ok
from tests.host.lifecycle_helpers import UID
from tests.host.live_helpers import world

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
ME = os.getpid()
GEN1, GEN2 = "a" * 32, "b" * 32


def wait_for(cond, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class Runner:
    """A runner in this process: supervisor + terminal socket + host link."""

    def __init__(
        self, w, state: Path, gen: str = GEN1, config: RunnerConfig | None = None, release: str | None = None
    ) -> None:
        self.sup = Supervisor(state, cgroups=None, scan_interval=0.2, exit_grace=0.5)
        self.sup.open()
        self.term = TermListener(
            Path(w.lab.run) / "term" / str(UID) / "term.sock", UID, gen=gen, info=self.sup.attach_info, sup=self.sup
        )
        self.term.open()
        # the H11a/H11b mechanics tests spawn /bin/sh directly: this runner's runner.json names it as a program
        # (H11c: the runner starts only registry launches and programs runner.json names)
        self.link = HostLink(
            self.sup,
            w.h.host.runner_path,
            host_uid=UID,
            gen=gen,
            term=self.term,
            backoff=(0.05, 0.2),
            config=config if config is not None else RunnerConfig(programs={"codex": "/bin/sh"}),
            grants=GrantStore(clock=lambda: w.lab.ws.clock[0]),  # the lab's clock, as the host's
            release=release,
        )
        self.grants = self.link.grants
        self.link.start()
        assert self.link.ready.wait(10), "the runner never got its hello answered"

    def close(self) -> None:
        self.link.stop()
        self.term.close()
        self.sup.close(stop_runs=True, grace=0.5)


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path, runner_identity=lambda cred: cred.pid == ME)
    yield w
    w.h.stop()
    w.lab.close()


@pytest.fixture
def rstate(tmp_path):
    d = tmp_path / "runner-state"
    d.mkdir(mode=0o700)
    return d


def payload(tmp_path: Path, argv: list[str]) -> dict:
    st = os.stat(tmp_path)
    return {
        "kind": "shell",
        "argv": argv,
        "env_names": ["PATH"],
        "env_fixed": {},
        "cwd": {"path": str(tmp_path), "dev": st.st_dev, "ino": st.st_ino},
        "cols": 80,
        "rows": 24,
    }


def fields(w, **kw) -> dict:
    return {"kind": "shell", "person": w.lab.owner, "ticket": None, "harness": "shell", **kw}


# -- the socket ------------------------------------------------------------------------------------------------------


def _release() -> str:
    from orch.host.service import _release_id

    return _release_id()


def _hello(path: Path) -> tuple[socket.socket, dict]:
    hello = {"t": "hello", "proto": [1], "client": "t", "caps": [], "ops": {}}
    return handshake_pinned(Pin(UID, None, path), hello)


def _first_frame(path: Path) -> dict:
    """Connect and read the host's first frame (a refused peer gets ``fatal`` before any hello)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)  # the identity check runs in a thread; slow under load
    try:
        s.connect(str(path))
        try:  # a refused peer may already be closed (Linux: EPIPE); its fatal frame is still readable
            s.sendall(frames.encode({"t": "hello", "proto": [1], "client": "t", "caps": [], "ops": {}}))
        except BrokenPipeError:
            pass
        dec = frames.Decoder()
        while True:
            got = dec.feed(s.recv(65536))
            if got:
                return got[0]
    finally:
        s.close()


def test_non_main_pid_runner_is_denied(tmp_path):
    w = world(tmp_path, runner_identity=lambda cred: False)
    try:
        frame = _first_frame(w.h.host.runner_path)
        assert frame["t"] == "fatal" and frame["error"]["code"] == "peer.denied"
    finally:
        w.h.stop()
        w.lab.close()


def test_runner_class_and_one_connection_per_uid(w):
    sock, welcome = _hello(w.h.host.runner_path)
    try:
        assert welcome["peer"]["class"] == "runner" and welcome["workspace"] is None
        assert welcome["caps"] == ["runner/1"] and set(welcome["ops"]) == {
            "runner.hello",
            "runner.report",
            "runner.exit",
            "runner.term",
        }
        frame = _first_frame(w.h.host.runner_path)
        assert frame["t"] == "fatal" and frame["error"]["code"] == "peer.denied"
    finally:
        sock.close()
    wait_for(lambda: not w.h.host.limits.runners)
    sock, _ = _hello(w.h.host.runner_path)  # the slot is free again once the first one closed
    sock.close()


def test_runner_ops_need_hello_first(w):
    sock, _ = _hello(w.h.host.runner_path)
    try:
        sock.sendall(
            frames.encode(
                {
                    "t": "req",
                    "id": "a1",
                    "op": "runner.exit",
                    "args": {
                        "run": new_run_id(),
                        "code": 0,
                        "signal": None,
                        "at": "2026-10-12T00:00:00Z",
                        "cleanup": "complete",
                    },
                }
            )
        )
        dec = frames.Decoder()
        got = []
        while not got:
            got = dec.feed(sock.recv(65536))
        assert got[0]["ok"] is False and got[0]["error"]["code"] == "op.not_allowed"
    finally:
        sock.close()


def test_runner_refuses_a_host_socket_of_another_uid(w, rstate):
    """The runner's peer-UID check of the host: a pin naming another UID never completes a handshake."""
    from orch.host.client import PinMismatch, connect_pinned

    with pytest.raises(PinMismatch):
        connect_pinned(Pin(UID + 1, None, w.h.host.runner_path))


def test_main_pid_parsing():
    def fake(out: str, code: int = 0):
        return lambda argv: subprocess.CompletedProcess(argv, code, out, "")

    assert (
        main_pid(501, fake("gui/501/io.orch.runner = {\n\tstate = running\n\tpid = 4242\n}\n"), platform="darwin")
        == 4242
    )
    assert main_pid(501, fake("", 113), platform="darwin") is None
    assert main_pid(1000, fake("777\n"), platform="linux") == 777
    assert main_pid(1000, fake("0\n"), platform="linux") is None
    ran = []
    main_pid(501, lambda argv: ran.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""), platform="darwin")
    assert ran == [["launchctl", "print", "gui/501/io.orch.runner"]]


def test_default_main_pid_never_reaches_the_service_manager():
    assert main_pid(os.getuid()) is None  # the H7 runner refuses under pytest: no launchctl, no systemctl


# -- spawn, dedupe, reconcile ------------------------------------------------------------------------------------------


def test_spawn_writes_intent_first_and_runs_once(w, rstate, tmp_path):
    r = Runner(w, rstate)
    try:
        hub = w.h.host.runners
        run = new_run_id()
        res = w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        assert res["run"] == run and res["state"] == "running"
        path = Path(w.lab.state) / "hosts" / w.wid / "runs" / f"{run}.json"
        doc = json.loads(path.read_text())
        assert doc["state"] == "running" and doc["runner_uid"] == UID and doc["gen"] == GEN1
        assert os.stat(path).st_mode & 0o777 == 0o600 and os.stat(path.parent).st_mode & 0o777 == 0o700
        assert "pid" not in res and "argv" not in json.dumps(res)  # the client view holds no pid, argv or env
        again = w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        assert again["run"] == run
        assert len([x for x in r.sup.runs() if x["run"] == run]) == 1
        stopped = w.h.run(hub.stop(run, 500))
        assert stopped == {"state": "exited", "cleanup": "complete"}
    finally:
        r.close()


def test_a_runner_of_another_release_is_refused_every_start(w, rstate, tmp_path):
    """A runner service left running across an upgrade applies its old rules (and env allow-list) to new requests:
    the host refuses the spawn with a plain message; nothing is relaxed and nothing is created."""
    r = Runner(w, rstate, release="old000000000")
    try:
        hub = w.h.host.runners
        assert hub.release == "old000000000"
        with pytest.raises(HostError) as err:
            w.h.run(hub.spawn(w.wid, fields(w, run=new_run_id()), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        assert err.value.code == "runner.stale" and "orch host enable" in err.value.message
        assert not list((Path(w.lab.state) / "hosts" / w.wid / "runs").glob("*.json"))  # no launch intent either
    finally:
        r.close()


def test_a_runner_that_predates_the_release_field_is_stale(w, rstate, tmp_path):
    sock, _ = _hello(w.h.host.runner_path)
    try:
        term = str(Path(w.lab.run) / "term" / str(UID) / "term.sock")
        args = {"version": "t", "gen": GEN1, "pid": ME, "term_socket": term, "runs": [], "grants": []}
        sock.sendall(frames.encode({"t": "req", "id": "h1", "op": "runner.hello", "args": args}))
        hub = w.h.host.runners
        wait_for(lambda: hub.connected)
        assert hub.release is None
        with pytest.raises(HostError) as err:
            hub.require_current()
        assert err.value.code == "runner.stale"
    finally:
        sock.close()


def test_lost_spawn_ack_gives_exactly_one_process(w, rstate, tmp_path):
    """The runner spawns but its report is lost: the host keeps the intent (``starting``), the reconnect's hello
    reconciles it to ``running``, and the runner never starts a second process."""
    hub = w.h.host.runners
    hub.command_timeout = 1.0
    r = Runner(w, rstate)
    try:
        r.link.drop_reports.add("spawn")
        run = new_run_id()
        marker = tmp_path / "count"
        with pytest.raises(HostError) as err:
            w.h.run(
                hub.spawn(
                    w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", f"echo x >> {marker}; sleep 60"])
                )
            )
        assert err.value.code == "outcome.unknown"
        assert hub.store.records[run]["state"] == "starting"
        r.link.drop_reports.clear()
        r.link.disconnect()  # a reconnect: a new hello reconciles
        wait_for(lambda: hub.store.records[run]["state"] == "running")
        time.sleep(0.3)
        assert marker.read_text() == "x\n"
        assert len([x for x in r.sup.runs() if x["run"] == run]) == 1
    finally:
        r.close()


def test_host_restart_reconciles_and_marks_missing_runs_lost(tmp_path, rstate):
    w = world(tmp_path, runner_identity=lambda cred: cred.pid == ME)
    r = None
    try:
        r = Runner(w, rstate)
        hub = w.h.host.runners
        keep = new_run_id()
        w.h.run(hub.spawn(w.wid, fields(w, run=keep), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        # a record the runner never heard of (as if the host crashed between intent and dispatch)
        ghost = new_run_id()
        rec = {**fields(w), "run": ghost, "workspace_id": w.wid, "state": "starting", "exit": None, "cleanup": None,
               "runner_uid": UID, "gen": GEN1, "dispatched": True}  # fmt: skip
        w.h.run(hub._save(rec))
        pid = next(x for x in r.sup.runs() if x["run"] == keep)["pid"]
        w.h.stop()  # the host restarts; the run keeps running
        assert procs.info(pid) is not None
        from tests.host.lifecycle_helpers import HostThread

        h2 = HostThread(w.lab.state, w.lab.run, pins=w.lab.pins, runner_identity=lambda cred: cred.pid == ME)
        h2.__enter__()
        try:
            hub2 = h2.host.runners
            wait_for(lambda: hub2.connected and hub2.store.records[ghost]["state"] == "lost", timeout=15)
            assert hub2.store.records[keep]["state"] == "running"
            assert hub2.store.records[ghost]["exit"]["reason"] == "lost"
            assert hub2.store.records[ghost]["cleanup"] == "unknown"
            assert hub2.epoch == 1  # a new authorization epoch (a new host instance)
        finally:
            h2.stop()
    finally:
        if r is not None:
            r.close()
        w.lab.close()


def test_runner_restart_marks_its_runs_runner_restarted(w, rstate, tmp_path):
    hub = w.h.host.runners
    r = Runner(w, rstate, gen=GEN1)
    run = new_run_id()
    w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
    # a crash: the link and I/O just stop (the process keeps running), then a new runner generation starts
    r.link.stop()
    r.term.close()
    r.sup._closing = True
    r.sup._wake()
    r.sup._thread.join()
    os.close(r.sup._lock_fd)  # a crash releases the runner lock
    r.sup._lock_fd = None
    pid, birth = next((x["pid"], x["birth"]) for x in r.sup.runs() if x["run"] == run)
    r2 = Runner(w, rstate, gen=GEN2)
    try:
        wait_for(lambda: hub.store.records[run]["state"] == "lost")
        assert hub.store.records[run]["exit"]["reason"] == "runner_restarted"
        r.sup.live[run].proc.wait(timeout=5)  # the crash sweep stopped it; reap our own child
        assert not procs.alive(pid, birth)
    finally:
        procs.signal_verified(pid, birth, 9)
        r2.close()


def test_spawn_environment_is_allow_listed():
    env = build_env(
        {"env_names": ["PATH", "HOME"], "env_fixed": {"ORCH_TICKET": "ABC-1"}},
        {"PATH": "/b", "HOME": "/h", "SECRET": "x"},
    )
    assert env == {
        "PATH": "/b",
        "HOME": "/h",
        "ORCH_TICKET": "ABC-1",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }
    for bad in (
        {"env_names": ["DYLD_INSERT_LIBRARIES"]},
        {"env_names": ["ORCH_GRANT"]},
        {"env_fixed": {"ORCH_GRANT": "x"}},
    ):
        with pytest.raises(SpawnError):
            build_env(bad, {})


def test_exit_reports_are_accepted_only_for_the_runners_own_runs(w, rstate, tmp_path):
    hub = w.h.host.runners
    r = Runner(w, rstate)
    try:
        foreign = new_run_id()
        rec = {**fields(w), "run": foreign, "workspace_id": w.wid, "state": "running", "exit": None, "cleanup": None,
               "runner_uid": UID + 7, "gen": GEN1}  # fmt: skip
        w.h.run(hub._save(rec))
        res = r.link.call(
            "runner.exit",
            {"run": foreign, "code": 0, "signal": None, "at": "2026-10-12T00:00:00Z", "cleanup": "complete"},
        )
        assert res["ok"] is False and res["error"]["code"] == "not_found"
        assert hub.store.records[foreign]["state"] == "running"
    finally:
        r.close()


def test_natural_exit_reaches_the_host(w, rstate, tmp_path):
    hub = w.h.host.runners
    r = Runner(w, rstate)
    try:
        run = new_run_id()
        w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "exit 4"])))
        wait_for(lambda: hub.store.records[run].get("cleanup") == "complete")
        rec = hub.store.records[run]
        assert rec["state"] == "exited" and rec["exit"]["code"] == 4 and rec["exit"]["reason"] == "exited"
    finally:
        r.close()


# -- the terminal socket ----------------------------------------------------------------------------------------------


def _recv_frame(s: socket.socket) -> dict:
    dec = frames.Decoder()
    while True:
        part = s.recv(65536)
        assert part, "the runner closed the connection"
        got = dec.feed(part)
        if got:
            return got[0]


def _attach(got: dict, nonce: str | None = None) -> tuple[socket.socket, dict, dict]:
    """Connect as the dashboard server: the runner's hello first, then the attach frame; returns the reply."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(got["socket"])
    hello = _recv_frame(s)
    s.sendall(frames.encode({"t": "attach", "ticket": got["ticket"], "nonce": nonce or got["nonce"]}))
    return s, hello, _recv_frame(s)


def test_attach_ticket_is_single_use_and_ended_by_the_host(w, rstate, tmp_path):
    hub = w.h.host.runners
    r = Runner(w, rstate)
    try:
        run = new_run_id()
        w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        got = w.h.run(hub.attach_allow(run, w.lab.owner, "read"))
        assert got["socket"] == str(r.term.path) == str(Path(w.lab.run) / "term" / str(UID) / "term.sock")
        assert got["runner"] == {"uid": UID, "pid": ME, "gen": GEN1} and len(got["ticket"]) == 43
        s, hello, ready = _attach(got)
        assert hello == {"t": "hello", "gen": GEN1, "pid": ME}  # what the server checks against got["runner"]
        assert ready["t"] == "ready" and ready["writer"] is False and ready["state"] == "running"
        assert {"start", "offset", "cols", "rows", "lease_gen"} <= set(ready)
        s2, _, again = _attach(got)
        assert again == {"t": "end", "reason": "ticket_invalid"}
        s2.close()
        w.h.run(hub.attach_end(got["att"], "person.removed"))
        assert _recv_frame(s) == {"t": "end", "reason": "person.removed"}
        s.close()
    finally:
        r.close()


def test_wrong_nonce_and_write_lease(w, rstate, tmp_path):
    hub = w.h.host.runners
    r = Runner(w, rstate)
    try:
        run = new_run_id()
        w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        bad = w.h.run(hub.attach_allow(run, w.lab.owner, "read"))
        s, _, refused = _attach(bad, nonce="0" * 32)
        assert refused == {"t": "end", "reason": "ticket_invalid"}
        s.close()
        first = w.h.run(hub.attach_allow(run, w.lab.owner, "write"))
        s1, _, ready1 = _attach(first)
        assert ready1["writer"] is True and ready1["lease_gen"] == 1
        busy = w.h.run(hub.attach_allow(run, w.lab.owner, "write", lease_gen=1))
        s2, _, refused = _attach(busy)
        assert refused == {"t": "end", "reason": "writer_busy"}  # take:false while the lease is held
        s2.close()
        take = w.h.run(hub.attach_allow(run, w.lab.owner, "write", take=True))
        s3, _, ready3 = _attach(take)
        assert ready3["writer"] is True and ready3["lease_gen"] == 2
        assert _recv_frame(s1) == {"t": "writer", "writer": False, "lease_gen": 2}
        s3.close()  # closing the holder releases the lease
        assert _recv_frame(s1) == {"t": "writer", "writer": False, "lease_gen": 3}
        s1.close()
    finally:
        r.close()


def test_host_loss_ends_attachments_and_refuses_tickets_until_hello(w, rstate, tmp_path):
    hub = w.h.host.runners
    r = Runner(w, rstate)
    try:
        run = new_run_id()
        w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        a = w.h.run(hub.attach_allow(run, w.lab.owner, "write"))
        b = w.h.run(hub.attach_allow(run, w.lab.owner, "read"))
        s, _, ready = _attach(a)
        assert ready["t"] == "ready"
        r.term.host_lost()  # what the link does when the host connection drops
        assert _recv_frame(s) == {"t": "end", "reason": "host.restarting"}
        s.close()
        s2, _, refused = _attach(b)  # minted in the old epoch
        assert refused == {"t": "end", "reason": "ticket_invalid"}
        s2.close()
        from orch.runner.term import TicketError

        with pytest.raises(TicketError):
            r.term.allow(_allow_args(run))
    finally:
        r.close()


def _allow_args(run: str, ticket: bytes = b"tk", expires: float | None = None, gen: str = GEN1) -> dict:
    import hashlib

    from orch.host.runners import _iso

    digest = hashlib.sha256(ticket).hexdigest()
    return {"att": "at_" + new_run_id()[2:], "ticket_hash": digest, "workspace_id": "0" * 32,
            "person": "p_" + "0" * 32, "run": run, "session": None, "mode": "read", "take": False, "lease_gen": 0,
            "runner_gen": gen, "from": None, "nonce": "1" * 32,
            "expires_at": _iso(expires if expires is not None else time.time() + 30)}  # fmt: skip


def test_unused_ticket_expires_and_lockout_counts_only_seen_hashes(tmp_path):
    from orch.runner.term import TicketError

    now = [time.time()]
    term = TermListener(Path(tmp_path) / "t" / "term.sock", os.getuid(), gen=GEN1, clock=lambda: now[0])
    term.host_ready(1)
    term.allow(_allow_args("r_x", expires=now[0] + 30))
    now[0] += 11  # a capability lives at most 10 s unused, whatever the host's expiry says
    assert term.consume("tk", "1" * 32) is None
    with pytest.raises(TicketError):
        term.allow(_allow_args("r_x", gen=GEN2))  # minted for another runner generation
    for i in range(20):  # random tickets never lock anybody out
        assert term.consume(f"random{i}", "1" * 32) is None
    assert term.locked_until == 0.0
    for _ in range(10):  # a replayed, already-seen capability does
        term.consume("tk", "1" * 32)
    assert term.locked_until > now[0]
    term.allow(_allow_args("r_y", ticket=b"fresh", expires=now[0] + 30))
    assert term.consume("fresh", "1" * 32) is None  # locked out for 60 s


# -- run.list / run.sub ------------------------------------------------------------------------------------------------


def test_run_sub_then_list_with_rev_and_runner_state(w, rstate, tmp_path):
    hub = w.h.host.runners
    c = w.conn()
    sub = ok(c.call("run.sub", {}))["data"]
    assert set(sub) == {"sub", "rev"}
    listed = ok(c.call("run.list", {}))["data"]
    assert listed["runs"] == [] and listed["next_page"] is None
    assert listed["runner"] == {"connected": False, "enabled": False, "terminals": False, "gen": None, "grant": None}
    enablement.set_enabled(Path(w.lab.state), w.wid, True, UID, terminals=False, text="t")
    r = Runner(w, rstate)
    try:
        run = new_run_id()
        w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        items = []
        while not any(i["t"] == "run" and i["run"]["state"] == "running" for i in items):
            items += c.evt(sub["sub"])["items"]
        revs = [int(i["id"].split(".")[0]) for i in items]
        assert revs == sorted(revs)
        run_item = items[-1]["run"]
        assert run_item["run"] == run and run_item["person"] == w.lab.owner and "pid" not in run_item
        assert run_item["rev"] == revs[-1] and run_item["exit"] is None and run_item["as_person"] is False
        listed = ok(c.call("run.list", {}))["data"]
        assert [x["run"] for x in listed["runs"]] == [run] and "out_bytes" in listed["runs"][0]
        assert listed["runner"] == {"connected": True, "enabled": True, "terminals": False, "gen": GEN1, "grant": None}
        assert listed["runs"][0]["rev"] >= revs[-1]
        assert ok(c.call("run.list", {"since": listed["runs"][0]["rev"]}))["data"]["runs"] == []
        w.h.run(hub.stop(run, 500))
        stopped = ok(c.call("run.list", {"state": "all"}))["data"]["runs"][0]
        assert stopped["exit"]["reason"] == "stopped" and stopped["exit"]["cleanup"] == "complete"
        assert ok(c.call("run.list", {}))["data"]["runs"] == []
        assert [x["run"] for x in ok(c.call("run.list", {"state": "all"}))["data"]["runs"]] == [run]
        ok(c.call("sub.close", {"sub": sub["sub"]}))
    finally:
        r.close()


def test_run_ops_column_rules(w, rstate, tmp_path):
    """G sees only its person's agent runs (never shells); another person's runs are invisible to P."""
    hub = w.h.host.runners
    c = w.conn()
    hidden_uid, hidden = w.ticket("not for the grant's person")
    w.restrict(hidden_uid, [w.quinn.ref])
    for rec in (
        {**fields(w), "run": new_run_id(), "kind": "shell"},
        {**fields(w), "run": new_run_id(), "kind": "agent", "harness": "codex", "ticket": w.ticket()[1]},
        {**fields(w), "run": new_run_id(), "kind": "agent", "person": w.quinn.ref},
        {**fields(w), "run": new_run_id(), "kind": "agent", "ticket": hidden},
    ):
        w.h.run(hub._save({**rec, "workspace_id": w.wid, "state": "running", "exit": None, "cleanup": None,
                           "runner_uid": UID, "gen": GEN1}))  # fmt: skip
    mine = ok(c.call("run.list", {}))["data"]["runs"]
    assert sorted(x["kind"] for x in mine) == ["agent", "shell"]  # not quinn's run, not the hidden ticket's
    grant = w.lab.ws.grant
    as_grant = ok(c.call("run.list", {}, grant=grant))["data"]["runs"]
    assert [x["kind"] for x in as_grant] == ["agent"]


# -- security scan follow-up -------------------------------------------------------------------------------------------


def _put(w, **kw):
    rec = {**fields(w), "run": new_run_id(), "workspace_id": w.wid, "state": "running", "exit": None,
           "cleanup": None, "runner_uid": UID, "gen": GEN1, **kw}  # fmt: skip
    w.h.run(w.h.host.runners._save(rec))
    return rec["run"]


def test_g_never_sees_ticketless_or_shell_runs_and_n_is_refused(w):
    c = w.conn()
    _put(w, kind="agent", ticket=None)
    _put(w, kind="shell")
    assert ok(c.call("run.list", {}, grant=w.lab.ws.grant))["data"]["runs"] == []
    for args in ({}, {"state": "all"}):
        res = c.call("run.list", args, grant="gr_bogus.notasecret")  # an unverified grant reads as N
        assert res["ok"] is False and res["error"]["code"] in ("op.not_allowed", "grant.required", "grant.expired")
    res = c.call("run.sub", {}, grant="gr_bogus.notasecret")
    assert res["ok"] is False


def test_hello_reports_naming_another_workspace_are_not_reports(w, rstate, tmp_path):
    """The runner reports a run of this host under another workspace id: the host does not take it as evidence."""
    hub = w.h.host.runners
    run = _put(w, state="starting")
    sock, _ = _hello(w.h.host.runner_path)
    try:
        rep = {"run": run, "workspace_id": "f" * 32, "state": "running", "pid": 1}
        term = str(Path(w.lab.run) / "term" / str(UID) / "term.sock")
        args = {"version": "t", "gen": GEN1, "pid": ME, "term_socket": term, "runs": [rep], "grants": []}
        sock.sendall(frames.encode({"t": "req", "id": "h1", "op": "runner.hello", "args": args}))
        wait_for(lambda: hub.store.records[run]["state"] != "starting")
        assert hub.store.records[run]["state"] == "lost"  # not reported for its own workspace: lost, never running
    finally:
        sock.close()


def test_host_stop_keeps_a_surviving_leader_running(w):
    hub = w.h.host.runners
    run = _put(w)

    async def survived(cmd, args, timeout=None):
        return {"state": "running", "cleanup": "incomplete"}

    hub.command = survived  # what the runner answers when even SIGKILL did not end the leader
    assert w.h.run(hub.stop(run, 100)) == {"state": "running", "cleanup": "incomplete"}
    rec = hub.store.records[run]
    assert rec["state"] == "running" and rec["cleanup"] == "incomplete" and rec["exit"] is None


def test_runner_peer_cannot_call_client_ops(w):
    sock, _ = _hello(w.h.host.runner_path)
    try:
        for i, op in enumerate(("run.list", "whoami", "new", "host.list")):
            sock.sendall(frames.encode({"t": "req", "id": f"x{i}", "op": op, "args": {}}))
        dec, got = frames.Decoder(), []
        while len(got) < 4:
            got += dec.feed(sock.recv(65536))
        assert all(f["ok"] is False and f["error"]["code"] == "unknown_command" for f in got)
    finally:
        sock.close()


def test_hello_must_name_the_connecting_pid_and_the_expected_term_socket(w):
    term = str(Path(w.lab.run) / "term" / str(UID) / "term.sock")
    for bad in ({"pid": ME + 1, "term_socket": term}, {"pid": ME, "term_socket": "/tmp/elsewhere.sock"}):
        sock, _ = _hello(w.h.host.runner_path)
        try:
            args = {"version": "t", "gen": GEN1, "runs": [], **bad}
            sock.sendall(frames.encode({"t": "req", "id": "h1", "op": "runner.hello", "args": args}))
            res = _recv_frame(sock)
            assert res["ok"] is False and res["error"]["code"] == "invalid.input"
            assert not w.h.host.runners.connected
        finally:
            sock.close()
        wait_for(lambda: not w.h.host.limits.runners)


# -- Opus and Sonnet review of #432 -------------------------------------------------------------------------------


def _recv_all(sock: socket.socket, dec: frames.Decoder) -> list:
    got: list = []
    while not got:
        got = dec.feed(sock.recv(65536))
    return got


def test_f1_a_stale_spawn_never_runs_after_the_reconnect_hello(w, rstate, tmp_path):
    """Opus F1 repro, now a regression test: a spawn queued on the old connection is dropped after the reconnect."""
    import threading

    hub = w.h.host.runners
    hub.command_timeout = 1.0
    r = Runner(w, rstate)
    gate = threading.Event()
    orig = r.link._command

    def slow(item, tag):  # the pool is busy (e.g. 4 stops in their grace) when the spawn arrives
        if item.get("op") == "spawn":
            gate.wait(20)
        orig(item, tag)

    r.link._command = slow
    try:
        run = new_run_id()
        with pytest.raises(HostError):
            w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "sleep 60"])))
        r.link.disconnect()  # reconnect: the hello's table has no such run
        wait_for(lambda: hub.store.records[run]["state"] == "lost")
        gate.set()  # the old connection's spawn would run now
        time.sleep(0.5)
        assert [x for x in r.sup.runs() if x["run"] == run] == []  # never started
        assert hub.store.records[run]["state"] == "lost"
    finally:
        gate.set()
        r.close()


def test_f2_a_new_generation_cannot_report_an_old_run_running(w):
    hub = w.h.host.runners
    run = _put(w, state="running")  # gen GEN1
    sock, _ = _hello(w.h.host.runner_path)
    dec = frames.Decoder()
    try:
        term = str(Path(w.lab.run) / "term" / str(UID) / "term.sock")
        rep = {"run": run, "workspace_id": w.wid, "state": "running", "pid": 4242}
        args = {"version": "t", "gen": GEN2, "pid": ME, "term_socket": term, "runs": [rep], "grants": []}
        sock.sendall(frames.encode({"t": "req", "id": "h1", "op": "runner.hello", "args": args}))
        res = _recv_all(sock, dec)[0]
        assert res["ok"] and set(res["result"]["data"]) == {"sub", "epoch"}
        rec = hub.store.records[run]
        assert rec["state"] == "lost" and rec["exit"]["reason"] == "runner_restarted" and rec.get("pid") is None
    finally:
        sock.close()


def test_f3_a_spawn_result_is_validated(w, tmp_path):
    import asyncio

    hub = w.h.host.runners
    c = w.conn()
    sock, _ = _hello(w.h.host.runner_path)
    dec = frames.Decoder()
    try:
        term = str(Path(w.lab.run) / "term" / str(UID) / "term.sock")
        args = {
            "version": "t",
            "release": _release(),
            "gen": GEN1,
            "pid": ME,
            "term_socket": term,
            "runs": [],
            "grants": [],
        }
        sock.sendall(frames.encode({"t": "req", "id": "h1", "op": "runner.hello", "args": args}))
        _recv_all(sock, dec)
        run = new_run_id()
        fut = asyncio.run_coroutine_threadsafe(
            hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "true"])), w.h.loop
        )
        evt = _recv_all(sock, dec)[0]
        cid = evt["items"][0]["cmd"]
        bad = {"pid": "x", "started_at": {"nested": ["a" * 1000] * 5}}
        report = {"cmd": cid, "outcome": "ok", "result": bad}
        sock.sendall(frames.encode({"t": "req", "id": "r1", "op": "runner.report", "args": report}))
        fut.result(10)
        got = ok(c.call("run.list", {}))["data"]["runs"][0]
        assert isinstance(got["started_at"], str) and len(got["started_at"]) <= 30
        assert hub.store.records[run].get("pid") is None
    finally:
        sock.close()


def test_f4_an_allow_from_an_earlier_connection_never_registers(tmp_path):
    from orch.runner.term import TicketError

    term = TermListener(Path(tmp_path) / "t" / "term.sock", os.getuid(), gen=GEN1)
    term.host_ready(2)
    with pytest.raises(TicketError):
        term.allow(_allow_args("r_x"), epoch=1)
    term.allow(_allow_args("r_x"), epoch=2)


def test_f5_a_revoked_grant_stops_receiving_run_items(w):
    hub = w.h.host.runners
    c = w.conn()
    one, two = w.ticket()[1], w.ticket("another")[1]
    _put(w, kind="agent", ticket=one)
    sub = ok(c.call("run.sub", {}, grant=w.lab.ws.grant))["data"]
    w.person_event("grant.revoked", grant=w.lab.ws.grant_id)
    _put(w, kind="agent", ticket=two)
    frame = c.evt(sub["sub"])
    assert frame.get("end") in ("grant.expired", "person.removed", "reset") and frame["items"] == []
    assert hub is w.h.host.runners


def test_f6_a_change_between_registration_and_seen_is_not_lost(w, monkeypatch):
    import asyncio

    from orch.host import runners

    hub = w.h.host.runners
    c = w.conn()
    real = runners.serve
    made = []
    calls = []

    def serve(call):
        calls.append(call.frame["op"])
        if calls.count("run.sub") == 2 and not made:  # published while the worker takes `seen` (after registering)
            rec = {**fields(w), "run": new_run_id(), "workspace_id": w.wid, "state": "running", "exit": None,
                   "cleanup": None, "runner_uid": UID, "gen": GEN1}  # fmt: skip
            asyncio.run_coroutine_threadsafe(hub._save(rec), w.h.loop).result(5)
            made.append(rec["run"])
        return real(call)

    monkeypatch.setattr(runners, "serve", serve)
    sub = ok(c.call("run.sub", {}))["data"]
    frame = c.evt(sub["sub"])
    assert [i["run"]["run"] for i in frame["items"]] == made


def test_hello_lists_every_running_run_first(tmp_path):
    """Sonnet B: more than 256 runs; every running one is reported, then the newest ended ones up to the cap."""
    from orch.runner.link import HostLink

    class FakeSup:
        on_event = None

        def __init__(self):
            self.rows = [{"run": new_run_id(), "state": "exited", "started_at": f"2026-10-12T10:{i % 60:02d}:00Z",
                          "workspace_id": "0" * 32} for i in range(300)]  # fmt: skip
            self.rows += [{"run": new_run_id(), "state": "running", "started_at": "2026-01-01T00:00:00Z",
                           "workspace_id": "0" * 32} for _ in range(5)]  # fmt: skip

        def runs(self):
            return list(self.rows)

        class table:  # noqa: N801
            @staticmethod
            def get(run):
                return {}

    link = HostLink(FakeSup(), tmp_path / "x.sock", host_uid=UID, gen=GEN1)
    runs = link._runs()
    assert len(runs) == 256 and [r["state"] for r in runs[:5]] == ["running"] * 5


def test_stop_failure_clears_stopping_and_late_exit_fills_the_code(w):
    hub = w.h.host.runners
    run = _put(w)

    async def fail(cmd, args, timeout=None):
        raise HostError("runner.unavailable", "gone", retry_after_ms=1000)

    hub.command = fail
    with pytest.raises(HostError):
        w.h.run(hub.stop(run, 100))
    assert not hub.store.records[run].get("stopping")

    async def done(cmd, args, timeout=None):
        return {"state": "exited", "cleanup": "complete"}

    hub.command = done
    assert w.h.run(hub.stop(run, 100)) == {"state": "exited", "cleanup": "complete"}
    assert hub.store.records[run]["exit"]["code"] is None  # the stop answer came before the exit report
    sock, _ = _hello(w.h.host.runner_path)
    dec = frames.Decoder()
    try:
        term = str(Path(w.lab.run) / "term" / str(UID) / "term.sock")
        args = {"version": "t", "gen": GEN1, "pid": ME, "term_socket": term, "runs": [], "grants": []}
        sock.sendall(frames.encode({"t": "req", "id": "h1", "op": "runner.hello", "args": args}))
        _recv_all(sock, dec)
        ex = {"run": run, "code": None, "signal": "SIGTERM", "at": "2026-10-12T00:00:00Z", "cleanup": "complete"}
        sock.sendall(frames.encode({"t": "req", "id": "e1", "op": "runner.exit", "args": ex}))
        assert _recv_all(sock, dec)[0]["ok"]
        exit_ = hub.store.records[run]["exit"]
        assert exit_["signal"] == "SIGTERM" and exit_["reason"] == "stopped" and not exit_.get("pending")
    finally:
        sock.close()


def test_evicted_runs_publish_gone(w, monkeypatch):
    from orch.host import runners

    monkeypatch.setattr(runners, "KEEP_ENDED", 1)
    c = w.conn()
    first = _put(w, state="exited", exit={"code": 0, "signal": None, "at": "x", "reason": "exited"},
                 cleanup="complete")  # fmt: skip
    sub = ok(c.call("run.sub", {}))["data"]
    _put(w, state="exited", exit={"code": 0, "signal": None, "at": "x", "reason": "exited"}, cleanup="complete")
    items = []
    while not any(i["t"] == "gone" for i in items):
        items += c.evt(sub["sub"])["items"]
    gone = next(i for i in items if i["t"] == "gone")
    assert {"t": "gone", "run": first} == {k: v for k, v in gone.items() if k != "id"}
    assert first not in w.h.host.runners.store.records
    # a fresh rev: higher than the evicted record's last one and than sub's start, below the record that evicted it
    assert int(gone["id"].split(".")[0]) > sub["rev"]
    newest = max(int(r.get("rev") or 0) for r in w.h.host.runners.store.records.values())
    assert int(gone["id"].split(".")[0]) < newest == w.h.host.runners.store.rev
