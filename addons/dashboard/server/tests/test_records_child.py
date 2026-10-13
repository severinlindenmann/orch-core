"""Bounded pipe reads plus a disposable Python child/grandchild; no git or remote starts."""

import json
import os
import signal
import subprocess
import sys
import threading

import pytest

from orch_dashboard import records_child as rc
from orch_dashboard.adapters import addons
from orch_dashboard.errors import HostError

WS = "a" * 32


class Process:
    def __init__(self, output=b'{"ok":true}\n', stderr=b"", code=0, hold=False):
        self.pid, self.returncode, self.code = 7654321, None, code
        self.writers = []
        for name, data in (("stdout", output), ("stderr", stderr)):
            read, write = os.pipe()
            setattr(self, name, os.fdopen(read, "rb"))
            # Small fixtures fit inside the kernel pipe without a writer thread.
            os.write(write, data)
            if hold:
                self.writers.append(write)
            else:
                os.close(write)
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.returncode = self.code
        return self.code

    def kill(self):
        for fd in self.writers:
            os.close(fd)
        self.writers.clear()


@pytest.fixture
def spawn(monkeypatch):
    calls, processes, kills = [], [], []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return processes.pop(0)

    monkeypatch.setattr(rc.subprocess, "Popen", factory)
    monkeypatch.setattr(rc.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(rc.os, "waitid", lambda kind, pid, flags: object())
    return calls, processes, kills


@pytest.mark.parametrize(
    "push,auto_off,command",
    [(False, False, ["commit"]), (True, False, ["commit", "--push"]), (False, True, ["auto", "off"])],
)
def test_exact_spawn_environment_and_fds(spawn, monkeypatch, tmp_path, push, auto_off, command):
    calls, processes, kills = spawn
    proc = Process()
    processes.append(proc)
    env = {
        "HOME": str(tmp_path),
        "PATH": "/bin",
        "LANG": "C",
        "SSH_AUTH_SOCK": "/tmp/fake-agent",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "ORCH_STATE_DIR": "relative-state",
    }
    monkeypatch.setattr(
        rc.os,
        "environ",
        {
            **env,
            "ORCH_GRANT": "never",
            "PYTHONPATH": "never",
            "ORCH_CLICK_FD": "99",
            "GIT_SSH_COMMAND": "never",
            "ORCH_RUN_DIR": "never",
            "ORCH_HOST_RUN_DIR": "never",
        },
    )
    assert rc.RecordsChild().run(
        WS, push=push, auto_off=auto_off, state=tmp_path / "state", run_dir=tmp_path / "host-run"
    ) == {"ok": True}
    args, kwargs = calls[0]
    assert args == ([sys.executable, "-I", "-m", "orch.records", *command, "--workspace", WS, "--json"],)
    assert kwargs == {
        "env": {
            **{k: env[k] for k in ("HOME", "PATH", "LANG", "SSH_AUTH_SOCK")},
            "ORCH_STATE_DIR": str(tmp_path / "state"),
            "ORCH_HOST_RUN_DIR": str(tmp_path / "host-run"),
        },
        "cwd": str(tmp_path / "state"),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "start_new_session": True,
        "close_fds": True,
    }
    assert kills == [(proc.pid, signal.SIGKILL)] and proc.stdout.closed and proc.stderr.closed


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"", 0),
        (b"not json", 0),
        (b"{}", 0),
        (b"[]", 0),
        (b'{"ok":true}\n{}\n', 0),
        (b'{"ok":true}\n\n', 0),
        (b'{"ok":true}', 3),
        (b'{"ok":1}', 0),
        (b'{"ok":false,"code":"records.busy","message":"busy"}', 3),
        (b'{"ok":false,"code":"invented","message":"bad"}', 1),
        (b'{"ok":false}', 1),
        (b'{"ok":true,"commit":"bad"}', 0),
        (b'{"ok":true,"ok":false}', 0),
        (b'{"ok":true,"url":null}', 0),
        (b'{"ok":true,"code":"records.busy"}', 0),
        (b"\xff", 0),
    ],
)
def test_bad_output_is_typed(raw, code):
    with pytest.raises(HostError) as error:
        rc.parse(raw, code)
    assert error.value.error["code"] == "records.child_invalid"


@pytest.mark.parametrize(
    "code,exit_code,retryable",
    [
        ("records.push_rejected", 3, False),
        ("records.busy", 9, True),
        ("sign.convenience_readers", 3, False),
        ("records.invalid", 5, False),
    ],
)
def test_catalog_errors(code, exit_code, retryable):
    data = {"ok": False, "code": code, "message": "refused", "commit": "a" * 40}
    with pytest.raises(HostError) as error:
        rc.parse(json.dumps(data).encode(), exit_code)
    assert error.value.exit == exit_code
    assert error.value.error == {
        "code": code,
        "message": "refused",
        "message_hidden": False,
        "retryable": retryable,
        "commit": "a" * 40,
    }


def test_untrusted_child_prose_and_as_built_extensions():
    got = rc.parse(b'{"ok":true,"url":"https://git.example.test/x","detail":{"private":true}}', 0)
    assert got == {"ok": True, "url": "https://git.example.test/x", "url_hidden": False}
    got = rc.parse(b'{"ok":true,"url":"spoof\\u202etext"}', 0)
    assert got["url"] is None and got["url_hidden"] is True
    with pytest.raises(HostError) as error:
        rc.parse(b'{"ok":false,"code":"records.busy","message":"bad\\u0000text","hint":"do not show"}', 9)
    assert error.value.error["message"] is None and error.value.error["message_hidden"] is True
    assert "hint" not in error.value.error


def test_timeout_kills_only_own_group_and_releases_busy(spawn, monkeypatch, tmp_path):
    calls, processes, kills = spawn
    proc = Process(hold=True)
    processes.append(proc)
    monkeypatch.setattr(rc, "BUDGET", 0.01)
    child = rc.RecordsChild()
    try:
        with pytest.raises(HostError) as error:
            child.run(WS, state=tmp_path)
        assert error.value.error["code"] == "records.budget"
        assert kills == [(proc.pid, signal.SIGKILL)]
        assert not child.active and proc.waits == [None]
        assert proc.stdout.closed and proc.stderr.closed
    finally:
        proc.kill()


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
def test_output_cap(spawn, monkeypatch, tmp_path, pipe):
    _, processes, kills = spawn
    monkeypatch.setattr(rc, "OUTPUT_LIMIT", 64)
    proc = Process(**({"output": b"x" * 65} if pipe == "stdout" else {"stderr": b"x" * 65}))
    processes.append(proc)
    with pytest.raises(HostError) as error:
        rc.RecordsChild().run(WS, state=tmp_path)
    assert error.value.error["code"] == "records.child_output_limit"
    assert kills == [(proc.pid, signal.SIGKILL)]


def test_one_per_workspace_and_other_workspaces_independent(monkeypatch, tmp_path):
    child = rc.RecordsChild()
    started, release = threading.Event(), threading.Event()

    def execute(argv, env):
        if WS in argv:
            started.set()
            assert release.wait(5)
        return {"ok": True}

    monkeypatch.setattr(child, "_execute", execute)
    thread = threading.Thread(target=child.run, args=(WS,), kwargs={"state": tmp_path})
    thread.start()
    try:
        assert started.wait(5)
        with pytest.raises(HostError) as error:
            child.run(WS, state=tmp_path)
        assert error.value.error["code"] == "records.busy"
        assert child.run("b" * 32, state=tmp_path) == {"ok": True}
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not child.active


def test_spawn_failure_and_workspace_validation(monkeypatch, tmp_path):
    def unavailable(*a, **k):
        raise OSError("private error")

    monkeypatch.setattr(rc.subprocess, "Popen", unavailable)
    child = rc.RecordsChild()
    for ws, code in [("--bad", "invalid.input"), (WS, "records.child_unavailable")]:
        with pytest.raises(HostError) as error:
            child.run(ws, state=tmp_path)
        assert error.value.error["code"] == code
        assert not child.active


def test_quick_count_projection():
    row = {
        "uid": "u",
        "key": "DEMO-0001",
        "title": "quick",
        "type": "chore",
        "status": "in_progress",
        "quick": {"commits": 0, "files": 2, "over": False},
    }
    got = addons.quick({"rows": [row], "total": 1, "next_page": "token"})
    assert got["rows"][0]["quick"] == row["quick"]
    assert got["rows"][0]["status"] == "in-progress"


def test_success_kills_grandchild_before_reaping(monkeypatch, tmp_path):
    # The grandchild closes output pipes but keeps a separate liveness pipe open.
    # EOF on that pipe proves it died, even if init has not reaped its zombie yet.
    import select

    live_read, live_write = os.pipe()
    real_popen, real_killpg = subprocess.Popen, os.killpg
    processes, order = [], []

    def spawn(argv, **kwargs):
        proc = real_popen(argv, **kwargs, pass_fds=(live_write,))
        processes.append(proc)
        os.close(live_write)
        real_wait = proc.wait

        def wait(*args, **kw):
            order.append("reap")
            return real_wait(*args, **kw)

        proc.wait = wait
        return proc

    def killpg(pid, sig):
        assert pid == processes[0].pid
        assert processes[0].returncode is None
        order.append("kill")
        real_killpg(pid, sig)

    monkeypatch.setattr(rc.subprocess, "Popen", spawn)
    monkeypatch.setattr(rc.os, "killpg", killpg)
    code = f"""
import os, time
if os.fork() == 0:
    os.close(1)
    os.close(2)
    time.sleep(30)
    os._exit(0)
os.close({live_write})
print('{{"ok":true}}', flush=True)
"""
    try:
        assert rc.RecordsChild()._execute([sys.executable, "-I", "-c", code], {"ORCH_STATE_DIR": str(tmp_path)}) == {
            "ok": True
        }
        assert order == ["kill", "reap"]
        assert select.select([live_read], [], [], 5)[0]
        assert os.read(live_read, 1) == b""
    finally:
        os.close(live_read)


def test_exit_wait_budget_keeps_leader_until_cleanup(spawn, monkeypatch, tmp_path):
    _, processes, kills = spawn
    proc = Process()
    processes.append(proc)
    monkeypatch.setattr(rc.os, "waitid", lambda *args: None)
    monkeypatch.setattr(rc, "BUDGET", 0.01)
    with pytest.raises(HostError) as error:
        rc.RecordsChild().run(WS, state=tmp_path)
    assert error.value.error["code"] == "records.budget"
    assert kills == [(proc.pid, signal.SIGKILL)]
    assert proc.waits == [None]
