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
    monkeypatch.setattr(rc, "exited", lambda pid, timeout: True)
    return calls, processes, kills


@pytest.mark.parametrize(
    "push,auto_off,status,command",
    [
        (False, False, False, ["commit"]),
        (True, False, False, ["commit", "--push"]),
        (False, True, False, ["auto", "off"]),
        (False, False, True, ["status"]),
    ],
)
def test_exact_spawn_environment_and_fds(spawn, monkeypatch, tmp_path, push, auto_off, status, command):
    calls, processes, kills = spawn
    proc = Process(status_output() if status else b'{"ok":true}\n')
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
        WS, push=push, auto_off=auto_off, status=status, state=tmp_path / "state", run_dir=tmp_path / "host-run"
    ) == (addons.records(status_data()) if status else {"ok": True})
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
    monkeypatch.setattr(rc, "exited", lambda pid, timeout: False)
    monkeypatch.setattr(rc, "BUDGET", 0.01)
    with pytest.raises(HostError) as error:
        rc.RecordsChild().run(WS, state=tmp_path)
    assert error.value.error["code"] == "records.budget"
    assert kills == [(proc.pid, signal.SIGKILL)]
    assert proc.waits == [None]


def test_a_real_child_that_exits_normally_without_grandchildren_is_parsed_not_an_error(tmp_path):
    """Opus M6 re-check N1: on macOS killpg of a group whose only member is the unreaped leader raises EPERM; a
    normal run must still be parsed (and the zombie reaped, the pipes closed)."""
    import json
    import sys

    from orch_dashboard.records_child import RecordsChild

    line = json.dumps({"ok": True, "commit": "a" * 40})
    env = {"PATH": "/usr/bin:/bin", "ORCH_STATE_DIR": str(tmp_path)}
    out = RecordsChild()._execute([sys.executable, "-c", f"print({line!r})"], env)
    assert out["ok"] is True


def test_a_real_child_that_outlives_the_budget_is_killed_and_reaped(monkeypatch, tmp_path):
    """The exit wait itself is bounded on every platform (waitid, or kqueue on macOS, which has no waitid)."""
    import sys

    from orch_dashboard.records_child import RecordsChild

    monkeypatch.setattr(rc, "BUDGET", 0.5)
    env = {"PATH": "/usr/bin:/bin", "ORCH_STATE_DIR": str(tmp_path)}
    code = "import os, time; os.close(1); os.close(2); time.sleep(30)"
    with pytest.raises(HostError) as error:
        RecordsChild()._execute([sys.executable, "-c", code], env)
    assert error.value.error["code"] == "records.budget"


def test_exited_sees_an_exit_without_reaping():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert rc.exited(proc.pid, 10)
        assert proc.returncode is None and rc.exited(proc.pid, 0)  # still a zombie: not reaped, seen again
    finally:
        assert proc.wait(timeout=10) == 0
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not rc.exited(sleeper.pid, 0.1)
    finally:
        sleeper.kill()
        sleeper.wait()


def status_data():
    return {
        "configured": True,
        "repo": {"state": "index_differs"},
        "pending": {"events": 0, "tickets": [], "more": False, "workspace_events": 0},
        "last": None,
        "auto": None,
        "remote": {
            "url": "https://git.example.test/old",
            "private": False,
            "export": "ok",
            "current_url": "https://git.example.test/new",
            "current_private": True,
            "matches_last": False,
        },
    }


def status_output():
    return json.dumps({"ok": True, "detail": {"status": status_data()}}).encode()


def test_status_projection_and_hidden():
    data = status_data()
    data["local"] = {"url": "never forward"}
    data["remote"]["extra"] = "never forward"
    data["remote"]["current_url"] = "bad\u202etext"
    out = rc.parse(json.dumps({"ok": True, "detail": {"status": data}}).encode(), 0, status=True)
    assert "local" not in out and "extra" not in out["remote"]
    assert out["remote"]["current_url"] is None and out["remote"]["current_url_hidden"]
    assert out["remote"]["current_private"] is True
    assert out["remote"]["matches_last"] is False


@pytest.mark.parametrize("field,value", [("current_private", 1), ("matches_last", "false"), ("current_url", {})])
def test_status_strict_remote(field, value):
    data = status_data()
    data["remote"][field] = value
    with pytest.raises(HostError, match="records.child_invalid"):
        rc.parse(json.dumps({"ok": True, "detail": {"status": data}}).encode(), 0, status=True)


@pytest.mark.parametrize("extra", [{"commit": "a" * 40}, {"detail": {}}, {"detail": {"status": {}, "other": 1}}])
def test_status_strict_envelope(extra):
    with pytest.raises(HostError, match="records.child_invalid"):
        rc.parse(json.dumps({"ok": True, "detail": {"status": status_data()}, **extra}).encode(), 0, status=True)


def test_status_error_catalog():
    with pytest.raises(HostError) as error:
        rc.parse(b'{"ok":false,"code":"records.busy","message":"busy"}', 9, status=True)
    assert error.value.exit == 9 and error.value.error["retryable"]


@pytest.mark.parametrize(
    "action,url,command",
    [
        ("init", None, ["init"]),
        (
            "mark",
            "https://git.example.test/r",
            ["remote-private", "--yes", "--expect-url=https://git.example.test/r"],
        ),
        ("unmark", None, ["remote-private", "--off"]),
        (
            "push-initial",
            "https://git.example.test/r",
            ["push", "--initial", "--yes", "--expect-url=https://git.example.test/r"],
        ),
    ],
)
def test_c15_admin_actions_are_fixed_argv(spawn, monkeypatch, tmp_path, action, url, command):
    calls, processes, kills = spawn
    processes.append(Process(b'{"ok":true}\n'))
    monkeypatch.setattr(rc.os, "environ", {"HOME": str(tmp_path), "PATH": "/bin"})
    assert rc.RecordsChild().run(WS, action=action, url=url, state=tmp_path / "s", run_dir=tmp_path / "r") == {
        "ok": True
    }
    assert calls[0][0] == ([sys.executable, "-I", "-m", "orch.records", *command, "--workspace", WS, "--json"],)


@pytest.mark.parametrize(
    "action,url",
    [
        ("shell", None),
        ("init", "https://a.example/r"),  # an action that takes no address is given none
        ("mark", None),  # an action that needs the reviewed address cannot run without it
        ("push-initial", None),
        ("mark", "a b"),
        ("mark", "a\nb"),
        ("mark", "x" * 2049),
        ("mark", "x‮y"),
    ],
)
def test_c15_admin_actions_refuse_anything_else(spawn, action, url):
    calls, processes, _ = spawn
    with pytest.raises(HostError) as info:
        rc.RecordsChild().run(WS, action=action, url=url)
    assert info.value.error["code"] == "invalid.input" and not calls


def test_c15_an_address_that_starts_with_a_dash_is_one_argv_element(spawn, monkeypatch, tmp_path):
    calls, processes, _ = spawn
    processes.append(Process(b'{"ok":true}\n'))
    monkeypatch.setattr(rc.os, "environ", {"HOME": str(tmp_path), "PATH": "/bin"})
    rc.RecordsChild().run(WS, action="mark", url="-oProxyCommand=x", state=tmp_path / "s", run_dir=tmp_path / "r")
    argv = calls[0][0][0]
    assert "--expect-url=-oProxyCommand=x" in argv and "-oProxyCommand=x" not in argv
