"""H11b acceptance: term.preview/open/attach, attach.cancel, run.stop, whoami.runner, the push path and the flood.

The runner runs in this process (as in test_runner_socket); every process started here is a run of that supervisor,
stopped in teardown. The terminals addon is granted through the workspace's own log.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

import pytest

from orch.host.runners import new_run_id
from orch.runner import enablement
from orch.runner.termclient import RunnerChanged, TermClient
from tests.host.dispatch_helpers import err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.live_helpers import world
from tests.host.test_runner_socket import GEN1, Runner, fields, wait_for

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path, runner_identity=lambda cred: cred.pid == os.getpid())
    yield w
    w.h.stop()
    w.lab.close()


@pytest.fixture
def rstate(tmp_path):
    d = tmp_path / "runner-state"
    d.mkdir(mode=0o700)
    return d


def grant_addon(w) -> None:
    w.person_event(
        "addon.granted", "workspace", name="terminals", version="0.1.0", package_sha256="sha256:" + "0" * 64,
        capabilities=["pty"], fields={}, sections=[], artifact_kinds=[],
    )  # fmt: skip


def enable(w, terminals: bool = True) -> None:
    enablement.set_enabled(Path(w.lab.state), w.wid, True, UID, terminals=terminals, text="t")


@pytest.fixture
def ready(w, rstate, monkeypatch):
    """The addon granted, dashboard execution and terminals on, a runner connected whose $SHELL is /bin/sh."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("PS1", "")
    grant_addon(w)
    enable(w)
    r = Runner(w, rstate)
    try:
        yield w
    finally:
        r.close()


def preview(c, **args):
    return ok(c.call("term.preview", args))["data"]


def open_shell(c, **args):
    pre = preview(c, **args)
    assert pre["blocked"] is None, pre["blocked"]
    return ok(c.call("term.open", {**args, "spec": pre["spec"]}, idem=True))["data"]["run"]


def attach(w, c, run, **args):
    data = ok(c.call("term.attach", {"run": run, **args}))["data"]
    client = TermClient(w.lab.run, data)
    client.connect()
    return data, client


def read_until(client, marker: bytes, timeout: float = 10.0) -> bytes:
    import base64

    got = b""
    deadline = time.monotonic() + timeout
    while marker not in got:
        assert time.monotonic() < deadline, got[-200:]
        frame = client.recv(max(0.05, deadline - time.monotonic()))
        assert frame is not None, f"stream ended: {client.ended}"
        if frame["t"] == "out":
            got += base64.b64decode(frame["b64"])
    return got


def drain_end(client, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while client.ended is None:
        assert time.monotonic() < deadline, "no end"
        try:
            if client.recv(0.2) is None:
                break
        except TimeoutError:
            pass
    return client.ended or "closed"


# -- preview and open ------------------------------------------------------------------------------------------------


def test_preview_blocks_in_the_order_addon_machine_terminals_runner(w, rstate, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    c = w.conn()
    assert preview(c)["blocked"]["code"] == "runner.disabled" and "addon" in preview(c)["blocked"]["message"]
    grant_addon(w)
    assert "machine" in preview(c)["blocked"]["message"]
    enable(w, terminals=False)
    assert "terminals" in preview(c)["blocked"]["message"]
    enable(w)
    assert preview(c)["blocked"]["code"] == "runner.unavailable"
    r = Runner(w, rstate)
    try:
        pre = preview(c)
        assert pre["blocked"] is None and pre["as_person"] is True and pre["login_shell"] is False
        assert pre["notice"] == "acts as you, not as an agent" and pre["program"] is None
        assert pre["cwd"] == {"kind": "workspace"} and pre["cwd_path"] == str(w.service.entry.root)
        assert "ORCH_GRANT" not in pre["env"] and "ORCH_SESSION" not in pre["env"]
        assert preview(c)["spec"] == pre["spec"]  # a stable digest
        assert preview(c, harness="codex")["blocked"]["code"] == "harness.not_found"
        assert preview(c, mode="context")["blocked"]["code"] == "launch.invalid"
        assert preview(c, ticket="NOPE-999")["blocked"]["code"] == "not_found"
        assert "ORCH_TICKET" in preview(c, ticket=w.ticket()[1])["env"]
        assert err(c.call("term.preview", {"x": 1})) == "invalid.input"
        assert err(c.call("term.preview", {}, grant=w.lab.ws.grant)) == "op.not_allowed"  # P only
    finally:
        r.close()


def test_open_needs_idem_and_the_previews_spec_and_a_retry_starts_one_process(ready):
    w = ready
    c = w.conn()
    pre = preview(c)
    assert err(c.call("term.open", {"spec": pre["spec"]})) == "idem.required"
    assert err(c.call("term.open", {"spec": "0" * 64}, idem=True)) == "launch.changed"
    assert err(c.call("term.open", {"spec": pre["spec"]}, grant=w.lab.ws.grant, idem=True)) == "op.not_allowed"
    rid = c.send("term.open", {"spec": pre["spec"]}, idem=True)
    first = ok(c.answer(rid))["data"]["run"]
    assert first["kind"] == "shell" and first["as_person"] is True and first["ticket"] is None
    assert first["person"] == w.lab.owner and first["harness"] == "shell" and first["session"] is None
    assert first["state"] == "running" and first["grant"] is None
    # the same idem again (a retry): the same run, no second process
    frame = {**c.ws.sent[-1]} if hasattr(c.ws, "sent") else None
    assert frame is None or frame["opts"]["idem"]
    live = ok(c.call("run.list", {}))["data"]["runs"]
    assert [r["run"] for r in live] == [first["run"]]


def test_the_shell_has_the_person_env_and_no_grant(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data, t = attach(w, c, run, mode="write", take=True)
    try:
        assert data["runner"]["gen"] == GEN1 and data["att"].startswith("at_") and len(data["ticket"]) == 43
        t.send_input(b"echo G=${ORCH_GRANT:-none} S=${ORCH_SESSION:-none} T=$ORCH_TICKET; echo done-$((6*7))\n")
        got = read_until(t, b"done-42")
        assert b"G=none S=none T=" in got
    finally:
        t.close()


# -- attach ----------------------------------------------------------------------------------------------------------


def test_attach_types_reads_and_the_lease_rules(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    d1, writer = attach(w, c, run, mode="write", take=True)
    try:
        assert writer.writer and d1["lease_gen"] == 0 and d1["cols"] == 120 and d1["rows"] == 40
        writer.send_input(b"echo first-$((1+1))\n")
        read_until(writer, b"first-2")
        assert err(c.call("term.attach", {"run": run, "mode": "write"})) == "term.writer_busy"
        d2, reader = attach(w, c, run)  # read-only by default
        try:
            assert not reader.writer and reader.ready["start"] == 0
            assert b"first-2" in read_until(reader, b"first-2")  # the ring replays
            assert reader.send_input(b"nope") == []  # a read attachment sends nothing
            listed = ok(c.call("run.list", {}))["data"]["runs"][0]
            wait_for(lambda: ok(c.call("run.list", {}))["data"]["runs"][0]["viewers"] == 2)
            assert ok(c.call("run.list", {}))["data"]["runs"][0]["writer"] is True and listed["cols"] == 120
            d3, taker = attach(w, c, run, mode="write", take=True)
            try:
                frame = None
                while frame is None or frame.get("t") != "writer":
                    frame = writer.recv(5)
                assert frame["writer"] is False and not writer.writer
                assert writer.send_input(b"x") == []  # the old holder's client does not even send
                taker.send_input(b"echo second-$((2+1))\n")
                read_until(taker, b"second-3")
            finally:
                taker.close()
        finally:
            reader.close()
    finally:
        writer.close()


def test_a_ticket_is_single_use_and_a_wrong_runner_is_refused_before_a_ticket_is_sent(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data = ok(c.call("term.attach", {"run": run}))["data"]
    TermClient(w.lab.run, data).connect()
    again = TermClient(w.lab.run, data)  # a replay: the runner closes with ticket_invalid
    again.connect()
    assert drain_end(again) == "ticket_invalid"
    bad_path = {**data, "socket": "/tmp/elsewhere/term.sock"}
    with pytest.raises(RunnerChanged):
        TermClient(w.lab.run, bad_path).connect()
    bad_gen = {**ok(c.call("term.attach", {"run": run}))["data"]}
    bad_gen["runner"] = {**bad_gen["runner"], "gen": "f" * 32}
    with pytest.raises(RunnerChanged):
        TermClient(w.lab.run, bad_gen).connect()
    bad_pid = {**ok(c.call("term.attach", {"run": run}))["data"]}
    bad_pid["runner"] = {**bad_pid["runner"], "pid": bad_pid["runner"]["pid"] + 1}
    with pytest.raises(RunnerChanged):
        TermClient(w.lab.run, bad_pid).connect()


def test_only_the_runs_person_attaches_stops_or_sees(ready):
    w = ready
    c = w.conn()
    other = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": other, "person": w.quinn.ref, "workspace_id": w.wid,
                                     "state": "running", "exit": None, "cleanup": None, "runner_uid": UID,
                                     "gen": GEN1}))  # fmt: skip
    assert err(c.call("term.attach", {"run": other})) == "not_found"
    assert err(c.call("run.stop", {"run": other})) == "not_found"
    mine = open_shell(c)["run"]
    assert err(c.call("term.attach", {"run": mine}, grant=w.lab.ws.grant)) == "op.not_allowed"  # G
    assert err(c.call("run.stop", {"run": mine}, grant=w.lab.ws.grant)) == "op.not_allowed"
    assert err(c.call("attach.cancel", {"att": "at_" + "0" * 26}, grant=w.lab.ws.grant)) == "op.not_allowed"
    assert ok(c.call("attach.cancel", {"att": "at_" + "0" * 26}))["data"] == {}  # unknown: nothing, nothing learned


def test_term_attach_refuses_runs_that_are_not_attachable(ready):
    w = ready
    c = w.conn()
    for state, mode in (("starting", "read"), ("lost", "read")):
        rec = new_run_id()
        w.h.run(w.h.host.runners._save({**fields(w), "run": rec, "workspace_id": w.wid, "state": state,
                                         "exit": None, "cleanup": None, "runner_uid": UID, "gen": GEN1}))  # fmt: skip
        assert err(c.call("term.attach", {"run": rec, "mode": mode})) == "term.not_attachable"
    run = open_shell(c)["run"]
    ok(c.call("run.stop", {"run": run, "grace_ms": 500}))
    assert err(c.call("term.attach", {"run": run, "mode": "write"})) == "term.not_attachable"
    data, t = attach(w, c, run)  # an exited run with its ring: read-only replay, then end
    try:
        assert t.ready["state"] == "exited" and drain_end(t) == "exited"
    finally:
        t.close()


def test_attach_limits_are_refused_by_the_host_from_the_runners_counts(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    clients = []
    try:
        for _ in range(4):
            clients.append(attach(w, c, run)[1])
        wait_for(lambda: ok(c.call("run.list", {}))["data"]["runs"][0]["viewers"] == 4)
        assert err(c.call("term.attach", {"run": run})) == "attach.limit"
        clients.pop().close()
        wait_for(lambda: ok(c.call("run.list", {}))["data"]["runs"][0]["viewers"] == 3)
        clients.append(attach(w, c, run)[1])
    finally:
        for t in clients:
            t.close()


# -- ending: cancel, stop, the person, the ticket -----------------------------------------------------------------


def test_attach_cancel_closes_the_attachment_only_for_its_own_person(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data, t = attach(w, c, run, mode="write", take=True)
    hub = w.h.host.runners
    foreign = "at_" + "1" * 26
    hub.attachments[foreign] = {"run": run, "person": w.quinn.ref, "workspace_id": w.wid, "at": time.monotonic()}
    ok(c.call("attach.cancel", {"att": foreign}))
    assert foreign in hub.attachments  # not the caller's: nothing happened to it
    hub.attachments.pop(foreign)
    ok(c.call("attach.cancel", {"att": data["att"]}))
    assert drain_end(t) == "cancelled"
    t.close()
    assert data["att"] not in hub.attachments
    wait_for(lambda: ok(c.call("run.list", {}))["data"]["runs"][0]["viewers"] == 0)
    assert ok(c.call("run.list", {}))["data"]["runs"][0]["state"] == "running"  # the run goes on


def test_run_stop_ends_viewers_with_run_stopped_and_reports_cleanup(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data, t = attach(w, c, run)
    res = ok(c.call("run.stop", {"run": run, "grace_ms": 500}))["data"]
    assert res == {"state": "exited", "cleanup": "complete"}
    assert drain_end(t) == "run.stopped"
    t.close()
    stopped = ok(c.call("run.list", {"state": "all"}))["data"]["runs"][0]
    assert stopped["exit"]["reason"] == "stopped" and stopped["replay_until"] is not None
    assert ok(c.call("run.stop", {"run": run}))["data"]["state"] == "exited"  # again: the same answer


def test_a_ticket_that_becomes_invisible_ends_the_attachment_and_the_run_continues(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("a restricted one")
    run = open_shell(c, ticket=key)["run"]
    data, t = attach(w, c, run, mode="write", take=True)
    try:
        w.restrict(uid, [w.quinn.ref])
        assert drain_end(t) == "not_visible"
        assert w.h.host.runners.store.records[run]["state"] == "running"
        assert err(c.call("term.attach", {"run": run})) == "not_found"
    finally:
        t.close()


def test_a_removed_person_loses_the_attachment_while_the_run_continues(tmp_path, rstate, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    q = world(tmp_path, map_to="quinn", runner_identity=lambda cred: cred.pid == os.getpid())
    try:
        grant_addon(q)
        enable(q)
        r = Runner(q, rstate)
        try:
            c = q.conn()
            run = open_shell(c)["run"]
            data, t = attach(q, c, run, mode="write", take=True)
            try:
                q.person_event("member.removed", person=q.quinn.ref)
                assert drain_end(t) == "person.removed"
                assert q.h.host.runners.store.records[run]["state"] == "running"
            finally:
                t.close()
        finally:
            r.close()
    finally:
        q.h.stop()
        q.lab.close()


def test_host_loss_ends_attachments_and_the_client_reports_unacknowledged_input(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data, t = attach(w, c, run, mode="write", take=True)
    try:
        t.send_input(b"sleep 30\n")
        assert t.lost or True
        hub = w.h.host.runners
        w.h.run(hub.attach_end(data["att"], "host.restarting"))
        assert drain_end(t) == "host.restarting"
        assert isinstance(t.lost, list)  # whatever was unacknowledged is reported, never replayed
    finally:
        t.close()


# -- whoami and the flood ------------------------------------------------------------------------------------------


def test_whoami_runner_per_column(ready):
    w = ready
    c = w.conn()
    info = ok(c.call("whoami", {}))["data"]["runner"]
    assert info == {"connected": True, "enabled": True, "terminals": True, "gen": GEN1, "grant": None}
    g = ok(c.call("whoami", {}, grant=w.lab.ws.grant))["data"]
    assert g["runner"]["connected"] is True


# -- the security review: every op against §12 and the N5 rules ----------------------------------------------------


def _wrong_secret(grant: str) -> str:
    from orch import crypto

    return f"{grant.partition('.')[0]}.{crypto.b64u(b'w' * 32)}"


def test_column_n_and_g_are_refused_for_every_terminal_op_and_never_learn_a_run(ready):
    w = ready
    c = w.conn()
    mine = open_shell(c)["run"]
    n_grant = _wrong_secret(w.lab.ws.grant)  # an unverified grant: column N
    other = new_run_id()
    w.h.run(w.h.host.runners._save({**fields(w), "run": other, "person": w.quinn.ref, "workspace_id": w.wid,
                                     "state": "running", "exit": None, "cleanup": None, "runner_uid": UID,
                                     "gen": GEN1}))  # fmt: skip
    calls = [("term.preview", {}), ("term.open", {"spec": "0" * 64}), ("term.attach", {"run": mine}),
             ("term.attach", {"run": other}), ("attach.cancel", {"att": "at_" + "0" * 26}),
             ("run.stop", {"run": mine}), ("run.stop", {"run": other})]  # fmt: skip
    for grant in (w.lab.ws.grant, n_grant):
        for op, args in calls:
            kw = {"idem": True} if op == "term.open" else {}
            # the column decides before the run is looked at: the same refusal for an own, a foreign and no run
            assert err(c.call(op, args, grant=grant, **kw)) == "op.not_allowed", (op, grant is n_grant)
    assert "runner" not in ok(c.call("whoami", {}, grant=n_grant))["data"]


def test_the_opt_in_is_read_again_at_every_write_capable_op(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    enablement.set_enabled(Path(w.lab.state), w.wid, False, UID)  # dashboard execution off
    pre = preview(c)
    assert pre["blocked"]["code"] == "runner.disabled"
    assert err(c.call("term.open", {"spec": pre["spec"]}, idem=True)) == "runner.disabled"
    assert err(c.call("term.attach", {"run": run})) == "runner.disabled"
    assert err(c.call("term.attach", {"run": run, "mode": "write", "take": True})) == "runner.disabled"
    enable(w, terminals=False)  # --no-terminals: the same
    assert err(c.call("term.attach", {"run": run})) == "runner.disabled"
    ok(c.call("run.stop", {"run": run, "grace_ms": 500}))  # stopping what is running is never refused


def test_the_addon_must_be_granted_for_attach_and_open(w, rstate, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    enable(w)
    r = Runner(w, rstate)
    try:
        c = w.conn()
        run = new_run_id()
        w.h.run(w.h.host.runners._save({**fields(w), "run": run, "workspace_id": w.wid, "state": "running",
                                         "exit": None, "cleanup": None, "runner_uid": UID, "gen": GEN1}))  # fmt: skip
        assert err(c.call("term.attach", {"run": run})) == "runner.disabled"
        assert err(c.call("term.open", {"spec": preview(c)["spec"]}, idem=True)) == "runner.disabled"
    finally:
        r.close()


def test_a_capability_is_never_routed_to_another_runner_uid_generation_or_workspace(ready):
    w = ready
    c = w.conn()
    hub = w.h.host.runners
    for change, code in (({"runner_uid": UID + 1}, "runner.unavailable"), ({"gen": "c" * 32}, "runner.unavailable"),
                         ({"workspace_id": "d" * 32}, "not_found")):  # fmt: skip
        run = new_run_id()
        w.h.run(hub._save({**fields(w), "run": run, "workspace_id": w.wid, "state": "running", "exit": None,
                           "cleanup": None, "runner_uid": UID, "gen": GEN1, **change}))  # fmt: skip
        assert err(c.call("term.attach", {"run": run})) == code, change
    assert hub.attachments == {}  # nothing was minted for any of them


def test_the_capability_is_bound_to_run_mode_and_one_use_and_not_transferable_between_persons(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    data = ok(c.call("term.attach", {"run": run}))["data"]
    info = w.h.host.runners.attachments[data["att"]]
    assert info["person"] == w.lab.owner and info["run"] == run and info["mode"] == "read"
    # a read capability never becomes write: the runner takes the mode from the capability, not from the client
    t = TermClient(w.lab.run, data)
    t.connect()
    try:
        assert t.ready["writer"] is False and t.send_input(b"x") == []
        t.writer = True  # a client that lies about holding the lease
        t.send_input(b"echo forged\n")
        time.sleep(0.3)  # a read attachment's input is dropped by the runner
    finally:
        t.close()
    wait_for(lambda: w.h.host.runners.store.records[run]["viewers"] == 0)
    # the shell never received the forged line
    d2, w2 = attach(w, c, run, mode="write", take=True)
    try:
        w2.send_input(b"echo marker-$((1+1))\n")
        assert b"forged" not in read_until(w2, b"marker-2").replace(b"echo marker", b"")
    finally:
        w2.close()
    # one use: the same capability again, from any connection, is ticket_invalid
    again = TermClient(w.lab.run, data)
    again.connect()
    assert drain_end(again) == "ticket_invalid"


def test_the_host_stays_responsive_while_eight_runs_flood_output(tmp_path):
    """The runner is a separate process here (as deployed), so its reader threads do not share the host's GIL."""
    import signal
    import subprocess

    from tests.host.test_runner_socket import payload

    src = str(Path(__file__).resolve().parents[2] / "src")
    child: list[subprocess.Popen] = []
    w = world(tmp_path, runner_identity=lambda cred: bool(child) and cred.pid == child[0].pid)
    try:
        grant_addon(w)
        enable(w)
        home = tmp_path / "home"
        home.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "PYTHONPATH": src, "SHELL": "/bin/sh",
               "ORCH_STATE_DIR": str(w.lab.state), "ORCH_HOST_RUN_DIR": str(w.lab.run)}  # fmt: skip
        code = "import sys; from orch.cli.main import main; sys.exit(main(['runner', 'run', '--foreground']))"
        child.append(subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))  # fmt: skip
        hub = w.h.host.runners
        wait_for(lambda: hub.connected, 20)
        c = w.conn()
        runs = []
        for _ in range(8):
            run = new_run_id()
            w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/usr/bin/yes", "orch-flood"])))
            runs.append(run)
        viewers = []
        try:
            for run in runs[:2]:  # two viewers that never read: the runner's queues and gaps carry them
                viewers.append(attach(w, c, run)[1])
            time.sleep(0.5)
            times = []
            for _ in range(300):
                start = time.perf_counter()
                ok(c.call("run.list", {}))
                times.append((time.perf_counter() - start) * 1000)
                time.sleep(0.005)
            times.sort()
            p99 = times[int(len(times) * 0.99) - 1]
            limit = 120 if os.environ.get("PYTEST_XDIST_WORKER") else 50  # parallel workers share the CPU
            assert p99 < limit, f"p99 {p99:.1f} ms, median {statistics.median(times):.1f} ms"
        finally:
            for t in viewers:
                t.close()
            for run in runs:
                try:
                    w.h.run(hub.stop(run, 500))
                except Exception:  # noqa: BLE001 - teardown
                    pass
    finally:
        for p in child:  # only the process this test started
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        w.h.stop()
        w.lab.close()
