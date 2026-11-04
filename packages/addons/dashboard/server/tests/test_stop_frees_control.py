"""`orch-dashboard stop` frees the control socket at once. A stopping server can take a while (uvicorn waits for open
connections such as a tab's live stream); until it was gone, `ctl.sock` still accepted connections and never
answered, so the next `orch-dashboard open` timed out instead of starting a new server."""

import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from orch_dashboard import __main__ as cli

SLOW_SHUTDOWN = 3.0


@pytest.fixture
def state(monkeypatch):
    # unix socket paths hold about 100 bytes: a short private directory, not pytest's long tmp_path
    root = Path(
        tempfile.mkdtemp(prefix="od-", dir=Path.home() / ".cache" if (Path.home() / ".cache").is_dir() else None)
    )
    os.chmod(root, 0o700)
    monkeypatch.setenv("ORCH_STATE_DIR", str(root))
    monkeypatch.delenv("ORCH_HOST_RUN_DIR", raising=False)
    monkeypatch.delenv("ORCH_RUN_DIR", raising=False)
    monkeypatch.setenv("ORCH_DASHBOARD_CLICK", "off")
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def slow_server(monkeypatch):
    """uvicorn's run, replaced: serve until `should_exit`, then take SLOW_SHUTDOWN seconds to finish (a live stream)."""
    done = threading.Event()

    def run(self, sockets=None):
        while not self.should_exit:
            time.sleep(0.02)
        time.sleep(SLOW_SHUTDOWN)
        done.set()

    monkeypatch.setattr(uvicorn.Server, "run", run)
    return done


def start(state):
    thread = threading.Thread(target=cli.serve, daemon=True)
    thread.start()
    ctl = state / "dashboard" / "ctl.sock"
    for _ in range(200):
        try:
            cli.control(ctl, "launch")
            return thread, ctl
        except OSError:
            time.sleep(0.02)
    raise AssertionError("the server did not start")


def test_stop_frees_the_control_socket_before_the_shutdown_ends(state, slow_server):
    thread, ctl = start(state)
    assert cli.control(ctl, "stop") == {"ok": True}
    t0 = time.monotonic()
    while ctl.exists() and time.monotonic() - t0 < 1:
        time.sleep(0.02)
    assert not ctl.exists()  # gone while the server is still shutting down
    assert not slow_server.is_set()
    with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
        cli.control(ctl, "launch")  # a launcher starts a new server instead of timing out
    thread.join(timeout=SLOW_SHUTDOWN + 5)
    assert slow_server.is_set() and not thread.is_alive()


def test_a_late_cleanup_never_removes_a_new_servers_socket(state, slow_server):
    thread, ctl = start(state)
    cli.control(ctl, "stop")
    t0 = time.monotonic()
    while ctl.exists() and time.monotonic() - t0 < 1:
        time.sleep(0.02)
    newer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        newer.bind(str(ctl))  # the next server, started while the old one still shuts down
        thread.join(timeout=SLOW_SHUTDOWN + 5)
        assert not thread.is_alive()
        assert ctl.exists()  # the old server's cleanup left it alone
    finally:
        newer.close()


def test_the_shutdown_cuts_open_connections_after_a_grace_period(state, slow_server, monkeypatch):
    seen = {}
    real = uvicorn.Config.__init__

    def record(self, *a, **kw):
        seen.update(kw)
        real(self, *a, **kw)

    monkeypatch.setattr(uvicorn.Config, "__init__", record)
    thread, ctl = start(state)
    cli.control(ctl, "stop")
    thread.join(timeout=SLOW_SHUTDOWN + 5)
    assert seen["timeout_graceful_shutdown"] == cli.GRACE and 0 < cli.GRACE <= 10


# -- `orch-dashboard restart` ---------------------------------------------------------------------------------------


def opened(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "open_dashboard", lambda path="/", print_url=False: calls.append((path, print_url)))
    return calls


def test_restart_stops_the_running_server_then_opens(state, slow_server, monkeypatch):
    thread, ctl = start(state)
    calls = opened(monkeypatch)
    monkeypatch.setattr("sys.argv", ["orch-dashboard", "restart"])
    assert cli.main() == 0
    assert calls == [("/", False)]
    assert cli._free(ctl) and not slow_server.is_set()  # it opened without waiting for the old shutdown
    thread.join(timeout=SLOW_SHUTDOWN + 5)


def test_restart_with_nothing_running_just_opens(state, monkeypatch):
    calls = opened(monkeypatch)
    monkeypatch.setattr("sys.argv", ["orch-dashboard", "restart", "/w/INT/settings"])
    assert cli.main() == 0
    assert calls == [("/w/INT/settings", False)]


def test_restart_names_a_server_that_does_not_answer(state, monkeypatch, capsys):
    calls = opened(monkeypatch)

    def hung(path, command):
        raise TimeoutError("timed out")

    monkeypatch.setattr(cli, "control", hung)
    monkeypatch.setattr("sys.argv", ["orch-dashboard", "restart"])
    assert cli.main() == 1
    assert "pgrep -fl orch_dashboard" in capsys.readouterr().err and calls == []


def test_restart_gives_up_when_the_old_server_keeps_the_socket(state, monkeypatch, capsys):
    calls = opened(monkeypatch)
    monkeypatch.setattr(cli, "control", lambda path, command: {"ok": True})
    monkeypatch.setattr(cli, "_free", lambda path: False)
    monkeypatch.setattr(cli, "RESTART_WAIT", 0.2)
    monkeypatch.setattr(cli.restart_dashboard, "__defaults__", ("/", False, 0.2))
    monkeypatch.setattr("sys.argv", ["orch-dashboard", "restart"])
    assert cli.main() == 1
    assert "did not stop in time" in capsys.readouterr().err and calls == []


# -- r11-P01/P01b: a stop during setup keeps only the loopback setup page served, until setup ends ---------------------
# ctl.sock is retired at once even then: a new server's startup (stale_socket) refuses with "dashboard already running"
# while anything accepts on ctl.sock: the service that Finish starts would exit and the http.500 would come back.


@pytest.fixture
def setup_sock(state):
    """A live setup process: a listening unix socket at <state>/setup/setup.sock."""
    (state / "setup").mkdir(mode=0o700)
    path = state / "setup" / "setup.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(path, 0o600)  # what the setup server makes it
    sock.listen(64)
    stop = threading.Event()

    def drain():  # accept and drop, like a setup server that sees no request
        sock.settimeout(0.1)
        while not stop.is_set():
            with __import__("contextlib").suppress(OSError):
                sock.accept()[0].close()

    threading.Thread(target=drain, daemon=True).start()
    yield path, sock
    stop.set()
    sock.close()


@pytest.fixture
def bound(monkeypatch):
    from orch_dashboard.auth import Auth

    flag = {"on": True}
    monkeypatch.setattr(Auth, "has_setup", lambda self: flag["on"])
    monkeypatch.setattr(cli, "SETUP_POLL", 0.02)
    return flag


def gone(ctl):
    t0 = time.monotonic()
    while ctl.exists() and time.monotonic() - t0 < 1:
        time.sleep(0.02)
    return not ctl.exists()


def test_a_stop_while_a_setup_page_is_bound_frees_ctl_now_and_exits_when_setup_ends(
    state, slow_server, setup_sock, bound
):
    """setup's Finish starts the dashboard service, which stops this (launcher) server while it still serves the setup
    page and that very Finish request: ctl.sock goes at once and a new server's startup check does not see a running
    dashboard (the service can start); the page keeps working until the setup binding ends, then the server exits."""
    thread, ctl = start(state)
    assert cli.control(ctl, "stop") == {"ok": True}
    assert gone(ctl)
    cli.stale_socket(ctl)  # the service's startup check: no "dashboard already running"
    time.sleep(0.3)
    assert thread.is_alive() and not slow_server.is_set()  # still serving the setup page
    bound["on"] = False  # Finish answered 200 (or setup was cancelled): the binding ends
    thread.join(timeout=SLOW_SHUTDOWN + 5)
    assert slow_server.is_set() and not thread.is_alive()


def test_a_stop_during_setup_exits_once_the_setup_process_is_gone(state, slow_server, setup_sock, bound):
    thread, ctl = start(state)
    cli.control(ctl, "stop")
    time.sleep(0.3)
    assert thread.is_alive()
    path, sock = setup_sock
    sock.close()  # the setup process ended (Ctrl-C): the binding is never cleared, the socket stops answering
    thread.join(timeout=SLOW_SHUTDOWN + 5)
    assert not thread.is_alive()


@pytest.mark.parametrize("kind", ["file", "unlistened"])
def test_a_stale_setup_socket_never_holds_a_stop(state, slow_server, bound, kind):
    """r11-P01b: a plain file or a socket nobody listens on is not a running setup: the stop exits at once."""
    (state / "setup").mkdir(mode=0o700)
    path = state / "setup" / "setup.sock"
    keep = None
    if kind == "file":
        path.touch()
    else:
        keep = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        keep.bind(str(path))  # bound, never listening: connect is refused
    try:
        assert not cli.setup_alive(path)
        thread, ctl = start(state)
        t0 = time.monotonic()
        cli.control(ctl, "stop")
        thread.join(timeout=SLOW_SHUTDOWN + 5)
        assert not thread.is_alive() and time.monotonic() - t0 < SLOW_SHUTDOWN + 1.5
    finally:
        if keep is not None:
            keep.close()


def test_the_hold_ends_by_itself_at_the_deadline(state, slow_server, setup_sock, bound, monkeypatch):
    assert 0 < cli.SETUP_HOLD <= 300  # a few minutes at most
    monkeypatch.setattr(cli, "SETUP_HOLD", 0.4)
    thread, ctl = start(state)
    cli.control(ctl, "stop")
    time.sleep(0.2)
    assert thread.is_alive()
    thread.join(timeout=SLOW_SHUTDOWN + 5)
    assert not thread.is_alive()


def test_during_the_hold_the_network_listener_stops_at_once(state, setup_sock, bound, monkeypatch):
    """r11-P01b: only the loopback server (the setup page) stays during the hold; the network listener stops now."""
    exited = []

    def run(self, sockets=None):
        while not self.should_exit:
            time.sleep(0.02)
        exited.append(sockets[0].family)

    monkeypatch.setattr(uvicorn.Server, "run", run)
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", 0))
    except OSError:
        pytest.skip("no IPv6 loopback here")
    port = probe.getsockname()[1]
    probe.close()
    thread = threading.Thread(target=cli.serve, kwargs={"port": port, "listen": "::1"}, daemon=True)
    thread.start()
    ctl = state / "dashboard" / "ctl.sock"
    for _ in range(200):
        try:
            cli.control(ctl, "hello")
            break
        except OSError:
            time.sleep(0.02)
    cli.control(ctl, "stop")
    t0 = time.monotonic()
    while socket.AF_INET6 not in exited and time.monotonic() - t0 < 2:
        time.sleep(0.02)
    assert exited == [socket.AF_INET6] and thread.is_alive()  # network gone, loopback still serving
    bound["on"] = False
    thread.join(timeout=SLOW_SHUTDOWN + 5)
    assert not thread.is_alive()


def held_then(state, slow_server, change):
    """Stop while a live setup is bound, then apply ``change``: the hold must end (the server exits)."""
    thread, ctl = start(state)
    cli.control(ctl, "stop")
    time.sleep(0.3)
    assert thread.is_alive()
    change()
    thread.join(timeout=SLOW_SHUTDOWN + 5)
    assert not thread.is_alive()


def test_the_hold_needs_the_same_checks_as_the_setup_proxy(state, slow_server, setup_sock, bound, monkeypatch):
    """r11-P01b L1: a setup socket open to group/other is not a setup this server trusts: the hold ends."""
    path, _ = setup_sock
    assert cli.setup_alive(path)
    held_then(state, slow_server, lambda: os.chmod(path, 0o666))


def test_a_setup_socket_held_by_another_user_ends_the_hold(state, slow_server, setup_sock, bound, monkeypatch):
    from orch_dashboard.adapters import setup as adapter

    def other_user():
        def deny(sock, uid):
            raise PermissionError("peer.denied")

        monkeypatch.setattr(adapter, "check_uid", deny)

    held_then(state, slow_server, other_user)


def test_a_failing_liveness_check_still_ends_the_hold(state, slow_server, setup_sock, bound, monkeypatch):
    """r11-P01b I3: whatever the check raises, the hold ends (try/finally)."""

    def broken(*_a, **_k):
        raise RuntimeError("boom")

    held_then(state, slow_server, lambda: monkeypatch.setattr(cli, "setup_alive", broken))
