"""#1734: each spawn site that starts our own code or a person's tool gives the child a cleaned environment."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orch.cli import dashboard as dash
from orch.cli import publish as pub
from orch.click import spawn as click_spawn
from orch.prune import model as prune_model
from orch.prune import safe as prune_safe

BAD = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp/h",
    "ORCH_STATE_DIR": "/tmp/s",
    "PYTHONPATH": "rel:/abs",
    "DYLD_INSERT_LIBRARIES": "/x.dylib",
    "LD_PRELOAD": "/y.so",
}


def _clean(env, *, keeps_absolute_pythonpath=False):
    assert env is not None
    assert "DYLD_INSERT_LIBRARIES" not in env and "LD_PRELOAD" not in env
    if keeps_absolute_pythonpath:
        assert env.get("PYTHONPATH") in (None, "/abs")
    else:
        assert "PYTHONPATH" not in env


def _capture(monkeypatch, name="run"):
    seen: dict = {}

    class Fake:
        pid = 1
        returncode = 0
        stdout = ""

        def __init__(self, argv, **kw):
            seen["argv"], seen["env"] = argv, kw.get("env")

        def poll(self):
            return 0

    def fake(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(subprocess, name, Fake if name == "Popen" else fake)
    return seen


def test_publish_admin_tool_gets_a_clean_env(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    env = {**BAD, "ORCH_STATE_DIR": str(tmp_path)}
    install = tmp_path / "install"
    install.mkdir()
    (install / "install.json").write_text(json.dumps({"v": 1, "parts": {"publish": {"binary": "/bin/echo"}}}))
    assert pub._allow_on_local_server({"workspace": "w", "owner": "o"}, env) is True
    _clean(seen["env"])
    assert seen["env"]["ORCH_STATE_DIR"] == str(tmp_path)


def test_prune_leave_and_hub_joined_get_a_clean_env(monkeypatch):
    seen = _capture(monkeypatch)
    prune_model._leave(Path("/tmp/ws"), BAD)
    _clean(seen["env"])
    assert seen["env"]["ORCH_WORKSPACE"] == "/tmp/ws"
    seen.clear()
    prune_model._hub_joined(Path("/tmp/ws"), BAD)
    _clean(seen["env"])


def test_prune_safe_run_gets_a_clean_env(monkeypatch):
    seen = _capture(monkeypatch)
    prune_safe.safe_run(["/bin/echo", "x"], BAD, deny=[])
    _clean(seen["env"])


def test_click_signer_spawn_gets_a_clean_env(monkeypatch):
    seen = _capture(monkeypatch, "Popen")
    monkeypatch.setattr(click_spawn, "ClickClient", lambda parent, proc: (parent.close(), proc)[1])
    click_spawn.start(BAD)
    _clean(seen["env"])
    assert seen["argv"][1] == "-I"


def test_dashboard_probe_and_exec_get_a_clean_env(monkeypatch):
    seen = _capture(monkeypatch)
    assert dash._module_installed(BAD) is True
    _clean(seen["env"], keeps_absolute_pythonpath=True)
    assert "-P" in seen["argv"]
    execd: dict = {}
    monkeypatch.setattr(dash.os, "execve", lambda path, argv, env: execd.update(env=env))
    dash._exec(["/bin/true"], BAD)
    _clean(execd["env"], keeps_absolute_pythonpath=True)


def test_install_qr_gets_a_minimal_env(monkeypatch):
    from orch.install import cli as install_cli

    seen = _capture(monkeypatch)
    monkeypatch.setattr(install_cli.shutil, "which", lambda n: "/usr/bin/qrencode")
    monkeypatch.setattr(install_cli.os, "environ", BAD)
    install_cli._qr("orch://x")
    assert set(seen["env"]) <= {"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "PATH"}
