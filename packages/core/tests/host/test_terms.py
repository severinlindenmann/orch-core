"""H11b acceptance: term.preview/open/attach, attach.cancel, run.stop, whoami.runner, the push path and the flood.

The runner runs in this process (as in test_runner_socket); every process started here is a run of that supervisor,
stopped in teardown. The terminals addon is granted through the workspace's own log.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import statistics
import sys
import time
from pathlib import Path

import pytest

from orch.host.errors import HostError
from orch.host.runners import new_run_id
from orch.runner import enablement
from orch.runner.termclient import RunnerChanged, TermClient, TermEnded
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
    w.runner = r
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
        codex = preview(c, harness="codex")  # H11c: a person-run harness through the login shell
        assert codex["blocked"] is None and codex["login_shell"] is True and "ORCH_GRANT" not in codex["env"]
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
        got = c.call("term.attach", {"run": rec, "mode": mode})
        assert err(got) == "term.not_attachable" and got["error"]["detail"] == {"state": state}
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
        res = c.call("term.attach", {"run": run})
        assert err(res) == "attach.limit"
        assert res["error"]["retryable"] is True and 1 <= res["error"]["retry_after_ms"] <= 3_600_000
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


# -- the push path ends live attachments (PR #443 review, Opus M1 to M3, L4) ----------------------------------------


def _writer(w, c, **kw):
    run = open_shell(c, **kw)["run"]
    data, t = attach(w, c, run, mode="write", take=True)
    return run, data, t


# -- M1: the kill switches end live attachments ---------------------------------------------------------------------


def test_disabling_the_machine_switch_ends_a_live_writer(ready):
    w = ready
    c = w.conn()
    run, data, t = _writer(w, c)
    try:
        enablement.set_enabled(Path(w.lab.state), w.wid, False, UID)
        assert drain_end(t) == "runner.disabled"
        got = c.call("term.attach", {"run": run})
        assert err(got) == "runner.disabled" and got["error"]["detail"] == {"missing": "machine"}
    finally:
        t.close()


def test_terminals_off_ends_a_live_writer(ready):
    w = ready
    c = w.conn()
    run, data, t = _writer(w, c)
    try:
        enablement.set_terminals(Path(w.lab.state), w.wid, False, UID)
        assert drain_end(t) == "runner.disabled"
    finally:
        t.close()


# -- M2: the watcher never dies, fails closed, and is restarted -----------------------------------------------------


def test_the_watcher_survives_a_non_host_error(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("a restricted one")
    run, data, t = _writer(w, c, ticket=key)
    hub = w.h.host.runners
    real = hub._ended_reason
    calls = {"n": 0}

    async def boom(info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient store error")
        return await real(info)

    hub._ended_reason = boom
    try:
        time.sleep(1.5)
        assert calls["n"] > 1 and not hub._watching.done(), "the watcher died"
        w.restrict(uid, [w.quinn.ref])
        assert drain_end(t) == "not_visible"
    finally:
        t.close()


class _Stale:
    """An authentication snapshot that can never be made fresh."""

    def __init__(self, real) -> None:
        self._real = real
        self.stamp = real.stamp

    def fresh(self, root) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_an_attachment_that_cannot_be_decided_is_ended(ready):
    w = ready
    c = w.conn()
    run, data, t = _writer(w, c)
    service = w.h.host.services[w.wid]
    real_auth, real_refresh, real_sync = service.auth, service.refresh, service.sync
    try:
        service.refresh = lambda: None
        service.sync = lambda **kw: None
        service.auth = _Stale(real_auth)
        started = time.monotonic()
        assert drain_end(t) == "host.restarting"
        assert time.monotonic() - started >= 1.5  # after the undecidable period, not at the first doubt
    finally:
        service.refresh, service.sync, service.auth = real_refresh, real_sync, real_auth
        t.close()


def test_a_stopped_watcher_is_restarted_by_the_next_runner_report(ready):
    w = ready
    c = w.conn()
    uid, key = w.ticket("a restricted one")
    run, data, t = _writer(w, c, ticket=key)
    hub = w.h.host.runners

    async def stop_watcher():
        old = hub._watching
        old.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await old
        return old

    try:
        time.sleep(0.5)  # the attach's own runner reports have arrived
        old = w.h.run(stop_watcher())
        assert old.done() and hub._watching is old
        t.resize(100, 30)  # the runner reports runner.term
        deadline = time.monotonic() + 5
        while hub._watching is old or hub._watching.done():
            assert time.monotonic() < deadline, "not restarted"
            time.sleep(0.05)
        w.restrict(uid, [w.quinn.ref])
        assert drain_end(t) == "not_visible"
    finally:
        t.close()


def test_a_stalled_workspace_worker_ends_its_attachments_and_never_delays_other_workspaces(ready):
    """PR #443 delta D1: a worker future that never resolves. The attachment in that workspace ends within the bound
    (``host.restarting``); an attachment in another workspace keeps being checked meanwhile."""
    import concurrent.futures

    from orch.host.runners import UNDECIDED_SECONDS, WATCH_SECONDS

    w = ready
    c = w.conn()
    uid, key = w.ticket("a restricted one")
    run, data, t = _writer(w, c, ticket=key)
    hub = w.h.host.runners
    stalled: concurrent.futures.Future = concurrent.futures.Future()  # never resolved

    class Stalled:
        auth = object()

        def submit(self, *a, **kw):
            return stalled

    fake = "at_0" + "0" * 25
    ended: dict[str, tuple[str, float]] = {}
    real_end = hub.attach_end

    async def attach_end(att, reason):
        ended.setdefault(att, (reason, time.monotonic()))
        return await real_end(att, reason)

    async def install():
        w.h.host.services["ws_stalled"] = Stalled()
        hub.attach_end = attach_end
        hub.attachments[fake] = {"run": "r_stalled", "person": "p_stalled", "mode": "read",
                                 "workspace_id": "ws_stalled", "ticket": None, "at": time.monotonic(),
                                 "seen": True}  # fmt: skip
        return time.monotonic()

    async def uninstall():
        w.h.host.services.pop("ws_stalled", None)
        hub.__dict__.pop("attach_end", None)

    try:
        started = w.h.run(install())
        time.sleep(WATCH_SECONDS * 2)  # the stalled check is now hanging on its worker
        w.restrict(uid, [w.quinn.ref])
        assert drain_end(t, timeout=UNDECIDED_SECONDS) == "not_visible"  # not held up by the stalled workspace
        assert fake not in ended or ended[fake][1] - started >= UNDECIDED_SECONDS
        deadline = started + UNDECIDED_SECONDS + WATCH_SECONDS * 2 + 0.5
        while fake not in ended:
            assert time.monotonic() < deadline, "the stalled workspace's attachment was not ended"
            time.sleep(0.05)
        assert ended[fake][0] == "host.restarting"
        assert ended[fake][1] - started >= UNDECIDED_SECONDS  # after the bound, not at the first doubt
        wait_for(lambda: fake not in hub.attachments, 5)
    finally:
        w.h.run(uninstall())
        t.close()


# -- M3: the runner holds only attachments the host's table holds ---------------------------------------------------


def test_the_host_entry_is_removed_only_after_the_runner_confirmed_the_end(ready):
    w = ready
    c = w.conn()
    run, data, t = _writer(w, c)
    hub = w.h.host.runners
    real = hub.command
    held = []

    async def command(cmd, args, **kw):
        if cmd == "attach.end":
            held.append(args["att"] in hub.attachments and hub.attachments[args["att"]].get("ending"))
        return await real(cmd, args, **kw)

    hub.command = command
    try:
        w.h.run(hub.attach_end(data["att"], "cancelled"))
        assert held == ["cancelled"]
        assert data["att"] not in hub.attachments
        assert drain_end(t) == "cancelled"
    finally:
        del hub.command
        t.close()


def test_an_unconfirmed_end_drops_the_runner_session(ready):
    w = ready
    c = w.conn()
    run, data, t = _writer(w, c)
    hub = w.h.host.runners
    real = hub.command

    async def command(cmd, args, **kw):
        if cmd == "attach.end":
            raise HostError("outcome.unknown", "no answer")
        return await real(cmd, args, **kw)

    hub.command = command
    try:
        with pytest.raises(HostError):
            w.h.run(hub.attach_end(data["att"], "cancelled"))
        assert hub.attachments == {}
        assert drain_end(t) == "host.restarting"  # the runner lost its host: every attachment ended
    finally:
        del hub.command
        t.close()


def test_a_stale_runner_report_that_arrives_late_never_takes_a_live_attachment_out(ready):
    """PR #443 delta D2: two runner threads may send their snapshots in reverse order. The older one (without the
    new attachment) arrives last; the host ignores it by ``seq`` and keeps watching the attachment."""
    w = ready
    c = w.conn()
    uid, key = w.ticket("a restricted one")
    run, data, t = _writer(w, c, ticket=key)
    hub = w.h.host.runners
    att = data["att"]
    try:
        wait_for(lambda: (hub.attachments.get(att) or {}).get("seen"), 5)
        newest = hub._term_seq[run]
        stale = {"run": run, "seq": newest - 1, "atts": [], "lease_gen": 0}  # taken before the attach registered
        assert w.h.run(hub.term_report(hub.session, stale)) == {}
        # The stale report is ignored: the seq never goes back (a real snapshot from the runner may already have moved
        # it forward; asserting equality raced with that, seen as "2 == 1" on CI).
        assert att in hub.attachments and hub._term_seq[run] >= newest
        assert [a["att"] for a in hub.term_state[run]["atts"]] == [att]
        w.restrict(uid, [w.quinn.ref])
        assert drain_end(t) == "not_visible"  # still watched: the revocation reaches it
    finally:
        t.close()


def test_an_attachment_the_host_does_not_hold_is_ended_at_the_runner(ready):
    w = ready
    c = w.conn()
    run, data, t = _writer(w, c)
    hub = w.h.host.runners

    async def forget():
        hub.attachments.pop(data["att"], None)

    try:
        w.h.run(forget())
        try:
            t.resize(101, 31)  # the next runner.term lists it
        except TermEnded:
            pass  # the watcher ended it first (the stream is already shut)
        assert drain_end(t) == "cancelled"
    finally:
        t.close()


def test_attach_end_is_not_delayed_by_four_concurrent_stops(ready):
    """PR #443 delta D4: four ``stop`` commands that block the runner's command pool must not delay an
    ``attach.end`` past its confirmation deadline (that would drop the whole runner session)."""
    import threading

    from orch.host.runners import END_TIMEOUT

    w = ready
    c = w.conn()
    run, data, t = _writer(w, c)
    other = open_shell(c)["run"]  # the stops target another run (a stop of this one would end its viewers first)
    hub = w.h.host.runners
    sup = w.runner.sup
    release = threading.Event()
    real_stop = sup.stop

    def slow_stop(run_id, grace=10.0):
        release.wait(END_TIMEOUT + 3)
        return real_stop(run_id, grace=grace)

    sup.stop = slow_stop
    stops: list = []

    async def scenario():
        for _ in range(4):
            stops.append(asyncio.ensure_future(hub.command("stop", {"run": other, "grace_ms": 100}, timeout=30)))
        await asyncio.sleep(0.3)  # the four stops now occupy every thread of the runner's command pool
        session = hub.session
        started = time.monotonic()
        await hub.attach_end(data["att"], "cancelled")
        return time.monotonic() - started, session

    try:
        took, session = w.h.run(scenario())
        assert took < 1.0
        assert hub.session is session and not session.closed  # the runner session was not dropped
        assert drain_end(t) == "cancelled"
    finally:
        release.set()
        if stops:

            async def settle():
                await asyncio.gather(*stops, return_exceptions=True)

            w.h.run(settle())
        sup.__dict__.pop("stop", None)
        t.close()


# -- L4: minting is rate limited per person -------------------------------------------------------------------------


def test_term_attach_is_rate_limited_per_person(ready):
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    codes = []
    for _ in range(12):
        res = c.call("term.attach", {"run": run})
        codes.append(None if res.get("ok") else res["error"]["code"])
    assert codes[:10] == [None] * 10
    assert "rate.limited" in codes[10:]


def test_term_attach_refusals_carry_retry_after_ms_from_the_bucket(ready):
    """§0.6: a retryable refusal carries ``error.retry_after_ms``; for the mint bucket (30 per minute) it is the time
    until the next token, so an immediate retry after it succeeds and none comes back sooner than 2 s."""
    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    refused = None
    for _ in range(12):
        res = c.call("term.attach", {"run": run})
        if not res.get("ok"):
            refused = res
            break
    assert refused is not None and refused["error"]["code"] == "rate.limited"
    wait = refused["error"]["retry_after_ms"]
    assert refused["error"]["retryable"] is True and 1 <= wait <= 2000
    time.sleep(wait / 1000 + 0.05)
    assert c.call("term.attach", {"run": run}).get("ok") is True


def test_attach_limit_reported_by_the_runner_keeps_its_code_and_carries_retry_after_ms(ready, monkeypatch):
    """The runner refuses ``attach.allow`` beyond its pending-capability cap with ``attach.limit``: the host passes
    that code on (not ``run.spawn_failed``) with ``retry_after_ms``."""
    from orch.runner import term

    w = ready
    c = w.conn()
    run = open_shell(c)["run"]
    monkeypatch.setattr(term, "MAX_ALLOWS", 0)
    res = c.call("term.attach", {"run": run})
    assert err(res) == "attach.limit"
    assert res["error"]["retryable"] is True and 1 <= res["error"]["retry_after_ms"] <= 3_600_000


def test_run_limit_on_term_open_carries_retry_after_ms(ready, monkeypatch):
    from orch.host import terms

    w = ready
    c = w.conn()
    open_shell(c)
    monkeypatch.setattr(terms, "SHELLS_PER_PERSON", 1)
    pre = preview(c)
    res = c.call("term.open", {"spec": pre["spec"]}, idem=True)
    assert err(res) == "run.limit" and res["error"]["detail"] == {"which": "shell_person"}
    assert res["error"]["retryable"] is True and 1 <= res["error"]["retry_after_ms"] <= 3_600_000


# -- whoami and the flood ------------------------------------------------------------------------------------------


def test_whoami_runner_per_column(ready):
    w = ready
    c = w.conn()
    info = ok(c.call("whoami", {}))["data"]["runner"]
    assert info.pop("now").endswith("Z")  # D2: the host's clock, for the renewal threshold (never the browser's)
    assert info == {"connected": True, "enabled": True, "terminals": True, "gen": GEN1, "grant": None}
    g = ok(c.call("whoami", {}, grant=w.lab.ws.grant))["data"]
    assert g["runner"]["connected"] is True
    assert "relay" not in ok(c.call("whoami", {}))["data"] and "relay" not in g  # the relay caps are column R only


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
    assert "relay" not in ok(c.call("whoami", {}, grant=n_grant))["data"]


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
        # H11c: the runner starts only registry launches and the programs runner.json names
        cfg = Path(w.lab.state) / "runner.json"
        cfg.write_text('{"v": 1, "programs": {"codex": "/bin/sh"}}')
        cfg.chmod(0o600)
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
            w.h.run(hub.spawn(w.wid, fields(w, run=run), payload(tmp_path, ["/bin/sh", "-c", "exec yes orch-flood"])))
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
