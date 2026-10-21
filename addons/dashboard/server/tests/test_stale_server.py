"""`open` replaces a dashboard server started by another release (or one that closes on the request) through its own
`stop` command, once; a server of the same release is reused."""

import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from orch_dashboard import __main__ as cli
from orch_dashboard.hostlink import check_uid, encode, receive

#: The release these tests run as. `cli.code_digest()` hashes the package files and is cached for the whole process;
#: other tests clear that cache (and patch `__file__`), so reading it here, at import or in a test, made "same release"
#: depend on test order and timing. The tests are about the reuse decision, not the hash: pin it.
REAL = "same-release-0"


@pytest.fixture(autouse=True)
def pinned_release(monkeypatch):
    monkeypatch.setattr(cli, "code_digest", lambda: REAL)


@pytest.fixture
def state(monkeypatch):
    cache = Path.home() / ".cache"
    root = Path(tempfile.mkdtemp(prefix="od-", dir=cache if cache.is_dir() else None))
    os.chmod(root, 0o700)
    monkeypatch.setenv("ORCH_STATE_DIR", str(root))
    monkeypatch.delenv("ORCH_HOST_RUN_DIR", raising=False)
    monkeypatch.delenv("ORCH_RUN_DIR", raising=False)
    for name in cli.AGENT_ENV:
        monkeypatch.delenv(name, raising=False)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class FakeServer:
    """A control socket speaking the protocol; `release=None` is an old server that closes on unknown commands."""

    def __init__(self, path, release):
        self.path, self.release, self.seen = path, release, []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.stopped = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                try:
                    check_uid(conn, os.getuid())
                    cmd = receive(conn).get("command")
                    self.seen.append(cmd)
                    if cmd == "stop":
                        conn.sendall(encode({"ok": True}))
                        self.stopped = True
                    elif cmd == "hello" and self.release:
                        conn.sendall(encode({"release": self.release}))
                    elif cmd == "launch" and self.release == REAL:
                        conn.sendall(encode({"code": "c", "port": 1, "expires_at": 0}))
                    # anything else: close without a reply, like an older release
                except Exception:
                    pass
        self.sock.close()
        Path(self.path).unlink(missing_ok=True)


@pytest.fixture
def started(monkeypatch):
    calls = []

    class Done(Exception):
        pass

    def popen(*a, **k):
        calls.append(a)
        raise Done

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    return calls, Done


def server_at(state, release):
    d = state / "dashboard"
    d.mkdir(mode=0o700, exist_ok=True)
    return FakeServer(d / "ctl.sock", release)


@pytest.mark.parametrize("release", ["old-digest", None])
def test_stale_or_silent_server_is_stopped_once_and_replaced(state, started, release, capsys):
    calls, Done = started
    server = server_at(state, release)
    with pytest.raises(Done):
        cli.open_dashboard("/setup")
    assert server.seen.count("stop") == 1
    assert "launch" not in server.seen or release is None  # a stale one is never asked to launch
    assert len(calls) == 1  # a new server was started once
    assert "restarted the dashboard on the new release" in capsys.readouterr().out


def test_same_release_is_reused(state, started):
    calls, _ = started
    server = server_at(state, REAL)
    with pytest.raises(Exception) as exc:  # past the launch, the probe fails: we only care that nothing restarted
        cli.open_dashboard("/setup")
    assert "stop" not in server.seen and not calls
    assert "launch" in server.seen
    server.stopped = True
    del exc


def test_old_server_closing_on_launch_takes_the_restart_path(state, started, monkeypatch, capsys):
    calls, Done = started
    server = server_at(state, "old-digest")
    # An older server that answers `hello` is impossible; simulate one whose `hello` says same but launch closes.
    monkeypatch.setattr(cli, "code_digest", lambda: "old-digest")
    with pytest.raises(Done):
        cli.open_dashboard("/setup")
    assert server.seen == ["hello", "launch", "stop"] and len(calls) == 1
    assert "restarted the dashboard" in capsys.readouterr().out


def test_eof_is_a_plain_error_not_a_traceback(monkeypatch, capsys):
    def boom(*a, **k):
        raise EOFError("host closed connection")

    monkeypatch.setattr(cli, "open_dashboard", boom)
    monkeypatch.setattr(cli.sys, "argv", ["orch-dashboard", "open"])
    assert cli.main() == 1
    err = capsys.readouterr().err
    assert err.startswith("orch-dashboard:") and "Traceback" not in err
