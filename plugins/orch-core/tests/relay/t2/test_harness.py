"""The T2 harness's own guarantees, without a relay (these run in the main matrix): the child environment is an
allow-list, a port answering another instance is never adopted, teardown reaps exactly our process group, and a
missing or wrong relay checkout skips locally but fails in CI."""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from . import harness
from .harness import RelayProcess, relay_env
from .orphan import orphan_test


def _sleeper(tmp_path: Path, port: int | None = None) -> tuple[subprocess.Popen[bytes], int]:
    port = port or harness.free_port()
    p = harness.spawn(harness.fake_cmd(port), cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, log=tmp_path / "fake.log")
    return p, port


def test_the_child_environment_is_an_allow_list(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_RELAY_MAX_PUBLICATION_BYTES", "1")
    monkeypatch.setenv("ORCH_RELAY_ORIGIN", "https://relay.dev.severin.io")
    monkeypatch.setenv("VIRTUAL_ENV", "/elsewhere")
    monkeypatch.setenv("ORCH_GRANT", "x")
    env = relay_env(tmp_path, 4321, "abc", None)
    assert env["ORCH_RELAY_ORIGIN"] == "http://127.0.0.1:4321"
    assert env["ORCH_RELAY_DATA"] == str(tmp_path) and env["ORCH_RELAY_INSTANCE"] == "abc"
    assert "ORCH_RELAY_MAX_PUBLICATION_BYTES" not in env  # the 16 MiB default: unset, not inherited
    assert not {"VIRTUAL_ENV", "ORCH_GRANT"} & set(env)
    assert set(env) <= set(harness.PASS_ENV) | {"ORCH_RELAY_ORIGIN", "ORCH_RELAY_DATA", "ORCH_RELAY_INSTANCE"}
    assert relay_env(tmp_path, 4321, "abc", harness.INTERNAL_CAP)["ORCH_RELAY_MAX_PUBLICATION_BYTES"] == "4194304"
    assert harness.LIMITS == (4194304, None)


def test_a_port_answering_another_instance_is_never_adopted(tmp_path):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ok": True, "instance": "someone-else"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((harness.HOST, 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    proc, _ = _sleeper(tmp_path, srv.server_address[1])
    rp = RelayProcess(tmp_path, tmp_path, srv.server_address[1], "ours", None, proc, tmp_path / "log")
    try:
        assert rp.wait_healthy(5) is False
    finally:
        rp.stop()
        srv.shutdown()
        srv.server_close()


def test_teardown_reaps_our_process_group_and_reports_leaks(tmp_path):
    proc, port = _sleeper(tmp_path)
    rp = RelayProcess(tmp_path, tmp_path, port, "ours", None, proc, tmp_path / "log")
    assert rp.leaked()  # alive: a leak if it were left like this
    assert rp.wait_healthy(0.5) is False  # nothing answers: never "started"
    rp.stop()
    assert rp.leaked() == []
    assert proc.returncode is not None
    # something still accepting on the relay's port is a leak too
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((harness.HOST, rp.port))
        s.listen(1)
        assert rp.leaked() == [f"port {rp.port} still accepting"]
    assert rp.leaked() == []


def test_a_leak_is_reaped_and_reported(tmp_path):
    proc, port = _sleeper(tmp_path)
    rp = RelayProcess(tmp_path, tmp_path, port, "ours", None, proc, tmp_path / "log")
    rp.stopped = True  # as if teardown never stopped it
    assert rp.owns_group()
    leaks = rp.finish()
    assert f"process group {rp.pgid} alive" in leaks  # reported (the fixture fails on it) ...
    assert not harness.group_alive(rp.pgid) and rp.leaked() == []  # ... and reaped
    assert rp not in harness._STARTED


class _SameGroup:
    """A handle on a group someone else leads (no pipe of ours, never waited on): what a reused pgid looks like."""

    stdin = None

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None

    def wait(self, timeout: float) -> int:
        raise subprocess.TimeoutExpired("x", timeout)


def test_a_group_that_is_not_verifiably_ours_is_never_signalled(tmp_path):
    proc, port = _sleeper(tmp_path)
    rp = RelayProcess(tmp_path, tmp_path, port, "ours", None, proc, tmp_path / "log")
    other_port = port + 1 if port < 65535 else port - 1
    other = RelayProcess(tmp_path, tmp_path, other_port, "x", None, _SameGroup(rp.pgid), tmp_path / "l")  # type: ignore[arg-type]
    try:
        other.stopped = True
        assert not other.owns_group()  # another port's tag
        other.kill_leaked()
        assert harness.group_alive(rp.pgid)  # untouched
        other.port, other.started = port, time.time() + 3600  # members born before the spawn are not ours either
        assert not other.owns_group()
        other.kill_leaked()
        assert harness.group_alive(rp.pgid)
    finally:
        harness._STARTED[:] = [x for x in harness._STARTED if x is not other]
        rp.finish()
    assert not harness.group_alive(rp.pgid)


def test_reap_all_ends_every_group_this_process_started(tmp_path):
    procs = [_sleeper(tmp_path) for _ in range(2)]
    rps = [RelayProcess(tmp_path, tmp_path, port, "ours", None, p, tmp_path / "log") for p, port in procs]
    leaks = harness.reap_all()
    assert leaks == []  # stopped cleanly: nothing leaked
    assert not any(harness.group_alive(rp.pgid) for rp in rps) and harness._STARTED == []


def test_a_fake_group_ends_when_its_parent_dies_without_cleanup(tmp_path):
    orphan_test(["fake", str(tmp_path), "2"], tmp_path)


def _outcome(fn) -> BaseException | None:
    """What ``fn`` raised, skip and fail outcomes included (``pytest.raises`` would let a wrong outcome escape as the
    test's own skip)."""
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 - Skipped/Failed are BaseException
        return e
    return None


def test_no_relay_dir_skips_locally_and_fails_in_ci(monkeypatch):
    monkeypatch.delenv(harness.RELAY_DIR_ENV, raising=False)
    for k in harness.REQUIRED_ENVS:
        monkeypatch.delenv(k, raising=False)
    e = _outcome(harness.relay_dir)
    assert type(e) is pytest.skip.Exception and "ORCH_RELAY_DIR is unset" in str(e)
    for k in harness.REQUIRED_ENVS:
        monkeypatch.setenv(k, "1")
        e = _outcome(harness.relay_dir)
        assert type(e) is pytest.fail.Exception and "requires it" in str(e), (k, type(e))
        monkeypatch.delenv(k)


def test_a_relay_checkout_at_another_commit_fails(monkeypatch, tmp_path):
    for p in ("src/orch_relay/cli.py", "ref/orch_protocol_ref.py"):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    git += ["-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    monkeypatch.setenv(harness.RELAY_DIR_ENV, str(tmp_path))
    e = _outcome(harness.relay_dir)
    assert type(e) is pytest.fail.Exception and "SOURCE pins" in str(e)
    monkeypatch.setenv(harness.RELAY_DIR_ENV, str(tmp_path / "src"))
    e = _outcome(harness.relay_dir)
    assert type(e) is pytest.fail.Exception and "not an orch-relay checkout" in str(e)


def test_the_per_test_budget_is_a_hard_limit_and_keeps_an_outer_alarm(monkeypatch):
    from . import conftest as t2conf

    monkeypatch.setattr(t2conf, "TEST_BUDGET_S", 0.2)
    outer = signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.setitimer(signal.ITIMER_REAL, 50)  # an outer alarm (perl -e 'alarm N') already armed
    try:
        t0 = time.monotonic()
        with pytest.raises(t2conf.T2Timeout), t2conf._Budget():
            time.sleep(5)
        assert time.monotonic() - t0 < 2
        left, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 45 < left <= 50  # restored, minus the time spent
        assert signal.getsignal(signal.SIGALRM) == signal.SIG_IGN
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, outer)
