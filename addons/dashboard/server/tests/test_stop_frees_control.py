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
