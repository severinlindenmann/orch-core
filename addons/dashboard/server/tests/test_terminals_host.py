"""M7a against core's in-process host and runner (`termkit`): the routes, the lazy attach, the bridge and the lease."""

import asyncio
import json
import logging
import sys
import time

import pytest
from termkit import TermWorld, key, read_until, text, until_close, until_text

from orch_dashboard.terminals import close_code

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


@pytest.fixture
def tw(tmp_path, monkeypatch):
    try:
        world = TermWorld(tmp_path, monkeypatch)
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        pytest.skip(f"no unix sockets here: {exc}")
    yield world
    world.close()


def test_open_attach_type_and_read_back(tw):
    run = tw.open_shell()
    assert run["kind"] == "shell" and run["as_person"] is True and run["state"] == "running"
    listed = tw.get("/runs").json()
    assert [r["run"] for r in listed["runs"]] == [run["run"]] and listed["runner"]["connected"] is True
    with tw.connect(run["run"], mode="write", take=True) as ws:
        ready = json.loads(text(ws))
        assert ready["t"] == "ready" and ready["writer"] is True and ready["state"] == "running"
        assert set(ready) == {"t", "start", "offset", "cols", "rows", "writer", "state"}
        ws.send_bytes(key(b"echo canary-$((6*7))\n"))
        got, _ = read_until(ws, b"canary-42")
        assert b"canary-42" in got


def test_read_attachment_drops_input_and_sees_writer_false(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run, mode="write", take=True) as writer:
        assert json.loads(text(writer))["writer"] is True
        with tw.connect(run) as reader:  # read-only by default
            ready = json.loads(text(reader))
            assert ready["writer"] is False
            reader.send_bytes(key(b"echo from-reader-$((2*3))\n"))  # dropped at the sink
            writer.send_bytes(key(b"echo from-writer-$((3*3))\n"))
            got, _ = read_until(reader, b"from-writer-9")
            assert b"from-reader-6" not in got


def test_stop_ends_the_attachment_with_1000(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run) as ws:
        assert json.loads(text(ws))["t"] == "ready"
        res = tw.post(f"/runs/{run}/stop")
        assert res.status_code == 200, res.text
        assert res.json()["cleanup"] in ("complete", "incomplete", "unknown", None)
        code, texts = until_close(ws)
        assert code == 1000 and texts[-1]["t"] == "end"
        assert texts[-1]["reason"] in ("run.stopped", "exited")


def test_take_control_moves_the_lease_and_write_without_take_is_4409(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run, mode="write", take=True) as first:
        assert json.loads(text(first))["writer"] is True
        with tw.connect(run, mode="write") as busy:  # take:false while the lease is held
            code, texts = until_close(busy)
            assert code == 4409 and texts[-1]["t"] == "end"
            assert texts[-1].get("code") == "term.writer_busy" or texts[-1]["reason"] == "writer_busy"
        with tw.connect(run, mode="write", take=True) as second:
            assert json.loads(text(second))["writer"] is True
            assert until_text(first, "writer") == {"t": "writer", "writer": False}  # the old holder: "Take back"
            first.send_bytes(key(b"echo stale-$((4*4))\n"))  # dropped: no lease
            second.send_bytes(key(b"echo fresh-$((5*5))\n"))
            got, _ = read_until(second, b"fresh-25")
            assert b"stale-16" not in got


def test_writer_resize_reaches_readers_and_reader_resize_is_dropped(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run, mode="write", take=True) as writer, tw.connect(run) as reader:
        assert json.loads(text(writer))["writer"] is True
        ready = json.loads(text(reader))
        assert (ready["cols"], ready["rows"]) == (120, 40)
        reader.send_text(json.dumps({"t": "resize", "cols": 60, "rows": 10}))  # ignored
        writer.send_text(json.dumps({"t": "resize", "cols": 100, "rows": 30}))
        assert until_text(reader, "resize") == {"t": "resize", "cols": 100, "rows": 30}
        # The host's run record follows the runner's report, coalesced to 250 ms (§12.3.2): wait for it, bounded.
        deadline, size = time.monotonic() + 5, None
        while time.monotonic() < deadline:
            listed = {r["run"]: r for r in tw.get("/runs").json()["runs"]}
            size = (listed[run]["cols"], listed[run]["rows"])
            if size == (100, 30):
                break
            time.sleep(0.05)
        assert size == (100, 30)


def test_logout_closes_the_socket_with_4401_and_no_end(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run) as ws:
        assert json.loads(text(ws))["t"] == "ready"
        tw.client.post("/auth/logout", headers=tw.headers())
        t0 = time.monotonic()
        code, texts = until_close(ws, timeout=5)
        assert code == 4401 and all(t["t"] != "end" for t in texts) and time.monotonic() - t0 < 2


def test_exited_run_replays_then_exit_then_end(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run, mode="write", take=True) as ws:
        assert json.loads(text(ws))["writer"] is True
        ws.send_bytes(key(b"echo bye-$((8*8)); exit 3\n"))
        code, texts = until_close(ws)
        assert code == 1000 and texts[-1] == {"t": "end", "reason": "exited"}
        assert {"t": "exit", "code": 3, "signal": None} in texts
    with tw.connect(run) as again:
        ready = json.loads(text(again))
        assert ready["state"] == "exited" and ready["writer"] is False
        got, _ = read_until(again, b"bye-64")  # the ring, below ready.offset: replay
        code, texts = until_close(again)
        assert code == 1000 and texts[-1]["reason"] == "exited"


def test_a_stopped_run_lists_exit_and_cleanup(tw):
    run = tw.open_shell()["run"]
    res = tw.post(f"/runs/{run}/stop")
    assert res.status_code == 200 and set(res.json()) == {"state", "cleanup"}
    deadline = time.monotonic() + 10
    while True:
        row = {r["run"]: r for r in tw.get("/runs").json()["runs"]}[run]
        if row["state"] != "running" or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert row["state"] == "exited" and row["exit"]["reason"] == "stopped"
    assert set(row["exit"]) == {"code", "signal", "at", "reason", "cleanup"}
    assert "grant" not in row and "session" not in row and "person" not in row


def test_unknown_run_is_a_host_refusal_with_its_code(tw):
    with tw.connect("r_01M4Y46B485AXD4CBEJZ15Z1TV") as ws:
        code, texts = until_close(ws)
        assert texts[-1]["reason"] == "host.error" and code == close_code("host.error", texts[-1]["code"])
        assert code in (4403, 4422)


def test_runs_reach_the_live_stream(tw):
    """run.sub items arrive as `runs` events (subscribe first; the page lists after `ready`)."""
    from orch_dashboard.live import Live

    async def run():
        live = Live(tw.gateway, tw.auth)
        w, tab = live.attach(tw.ws, tw.token)
        stream = live.stream(w, tab)
        first = await asyncio.wait_for(anext(stream), 10)
        assert "event: ready" in first
        opened = await asyncio.to_thread(tw.open_shell)
        while True:
            frame = await asyncio.wait_for(anext(stream), 10)
            if "event: runs" in frame:
                data = json.loads(frame.split("data: ", 1)[1])
                if any(i["t"] == "run" and i["run"]["run"] == opened["run"] for i in data):
                    break
        await stream.aclose()
        await live.close()
        return data

    items = asyncio.run(run())
    assert all(set(i) == {"t", "run"} for i in items)


def test_terminal_bytes_never_reach_the_logs(tw, caplog, capfd):
    """Section 12.6.6 hygiene: a canary typed into the shell and printed by it is in no log line of this process."""
    canary = "CANARY-" + "7f3c9e"
    caplog.set_level(logging.DEBUG)
    run = tw.open_shell()["run"]
    with tw.connect(run, mode="write", take=True) as ws:
        assert json.loads(text(ws))["writer"] is True
        ws.send_bytes(key(f"echo {canary[:7]}''{canary[7:]}\n".encode()))
        got, _ = read_until(ws, canary.encode())
    with tw.connect(run) as reader:  # and once more as replay
        text(reader)
        read_until(reader, canary.encode())
    out, err = capfd.readouterr()
    logged = caplog.text + out + err
    assert canary not in logged and canary[:7] + "''" not in logged


def test_a_changed_runner_ends_with_4503_and_cancels_the_capability(tw, monkeypatch):
    """Section 12.6.1: any verification mismatch closes with runner.changed, and the minted capability is cancelled."""
    from orch.runner import termclient

    from orch_dashboard.hostlink import HostLink

    def changed(self, timeout=5.0):
        raise termclient.RunnerChanged("the runner's hello does not match")

    calls = []
    real_call = HostLink.call

    def spy(self, op, args=None, **kw):
        calls.append(op)
        return real_call(self, op, args, **kw)

    monkeypatch.setattr(termclient.TermClient, "connect", changed)
    monkeypatch.setattr(HostLink, "call", spy)
    run = tw.open_shell()["run"]
    with tw.connect(run) as ws:
        code, texts = until_close(ws)
    assert code == 4503 and texts == [{"t": "end", "reason": "runner.changed"}]
    assert calls.count("term.attach") == 1 and "attach.cancel" in calls


def slow_attach(monkeypatch, delay=1.5):
    """`term.attach` answers after `delay`; every HostLink op and every TermClient.connect is recorded."""
    from orch.runner import termclient

    from orch_dashboard.hostlink import HostLink

    calls, connects = [], []
    real_call, real_connect = HostLink.call, termclient.TermClient.connect

    def call(self, op, args=None, **kw):
        calls.append(op)
        if op == "term.attach":
            time.sleep(delay)
        return real_call(self, op, args, **kw)

    def connect(self, timeout=5.0):
        connects.append(1)
        return real_connect(self, timeout)

    monkeypatch.setattr(HostLink, "call", call)
    monkeypatch.setattr(termclient.TermClient, "connect", connect)
    return calls, connects


def test_logout_during_the_bind_closes_at_once_and_presents_nothing_review_l2(tw, monkeypatch):
    run = tw.open_shell()["run"]
    calls, connects = slow_attach(monkeypatch)
    with tw.connect(run, mode="write", take=True) as ws:
        time.sleep(0.2)  # the bind is waiting for term.attach
        tw.client.post("/auth/logout", headers=tw.headers())
        t0 = time.monotonic()
        code, texts = until_close(ws, timeout=5)
        assert code == 4401 and texts == [] and time.monotonic() - t0 < 1.0  # no ready, no end
    deadline = time.monotonic() + 5
    while "attach.cancel" not in calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert connects == [] and "attach.cancel" in calls  # the host ticket was never presented, the capability cancelled


def test_a_message_during_the_bind_closes_at_once_review_l3(tw, monkeypatch):
    run = tw.open_shell()["run"]
    calls, connects = slow_attach(monkeypatch)
    with tw.connect(run) as ws:
        time.sleep(0.2)
        ws.send_bytes(key(b"too early"))
        t0 = time.monotonic()
        code, texts = until_close(ws, timeout=5)
        assert code == 4400 and texts == [{"t": "end", "reason": "protocol"}] and time.monotonic() - t0 < 1.0
    deadline = time.monotonic() + 5
    while "attach.cancel" not in calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert connects == [] and "attach.cancel" in calls


def test_a_failed_connect_after_the_ticket_closes_the_runner_socket_review_l4(tw, monkeypatch):
    import socket as socketlib

    from orch.runner import termclient

    made = []

    def broken(self, timeout=5.0):
        self.sock = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        made.append(self.sock)
        raise TimeoutError("no frame")  # e.g. no first frame after the attach was sent

    monkeypatch.setattr(termclient.TermClient, "connect", broken)
    run = tw.open_shell()["run"]
    with tw.connect(run) as ws:
        code, _ = until_close(ws)
    assert code == 4503 and made and made[0].fileno() == -1
