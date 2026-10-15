"""The T2 harness's own guarantees, without a relay (these run in the main matrix): the child environment is an
allow-list, a port answering another instance is never adopted, teardown reaps exactly our process group, and a
missing or wrong relay checkout skips locally but fails in CI."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from . import harness
from .harness import RelayProcess, relay_env


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL, start_new_session=True
    )


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
    proc = _sleeper()
    rp = RelayProcess(tmp_path, tmp_path, srv.server_address[1], "ours", None, proc, tmp_path / "log")
    try:
        assert rp.wait_healthy(5) is False
    finally:
        rp.stop()
        srv.shutdown()
        srv.server_close()


def test_teardown_reaps_our_process_group_and_reports_leaks(tmp_path):
    proc = _sleeper()
    rp = RelayProcess(tmp_path, tmp_path, harness.free_port(), "ours", None, proc, tmp_path / "log")
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
