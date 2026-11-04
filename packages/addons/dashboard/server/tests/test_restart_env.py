"""#1734: the dashboard self-start uses -P and a cleaned env; the signer child gets no loader variable."""

import os
import subprocess
import sys

import pytest

import orch_dashboard.__main__ as launcher
from orch_dashboard import click_signer

BAD = {
    "PYTHONPATH": "rel:/abs",
    "DYLD_INSERT_LIBRARIES": "/x.dylib",
    "LD_PRELOAD": "/y.so",
    "PATH": "/usr/bin:/bin",
    "ORCH_STATE_DIR": "/tmp/s",
}


class _Started(Exception):
    pass


def test_self_start_uses_dash_p_and_a_clean_environment(tmp_path, monkeypatch):
    seen = {}

    def popen(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        raise _Started

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "stale_socket", lambda _p: None)
    monkeypatch.setattr(launcher, "_running_release", lambda _p: None)
    monkeypatch.setattr(os, "environ", dict(BAD))
    with pytest.raises(_Started):
        launcher.control_or_start(tmp_path, "hello")
    assert seen["argv"][:3] == [sys.executable, "-P", "-m"]
    env = seen["env"]
    assert "DYLD_INSERT_LIBRARIES" not in env and "LD_PRELOAD" not in env
    assert env.get("PYTHONPATH") == "/abs"  # the relative entry (the current directory) is gone
    assert env["ORCH_STATE_DIR"] == "/tmp/s"


def test_signer_child_env_has_no_loader_variable(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(BAD))
    env = click_signer.child_env(tmp_path)
    assert not {"PYTHONPATH", "DYLD_INSERT_LIBRARIES", "LD_PRELOAD"} & set(env)
    assert env["ORCH_STATE_DIR"] == str(tmp_path)


def test_a_shadow_module_in_the_current_directory_is_not_imported_by_the_restart(tmp_path, monkeypatch):
    """Run OUR restart argv prefix and env (captured from control_or_start) in a folder holding ``orch_dashboard.py``,
    with PYTHONPATH=":" (an empty entry: the current directory) in the parent: the shadow must not be imported."""
    seen = {}
    real_run = subprocess.run

    def popen(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        raise _Started

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "stale_socket", lambda _p: None)
    monkeypatch.setattr(launcher, "_running_release", lambda _p: None)
    monkeypatch.setattr(os, "environ", {**os.environ, "PYTHONPATH": ":"})
    with pytest.raises(_Started):
        launcher.control_or_start(tmp_path / "state", "hello")
    work = tmp_path / "work"
    work.mkdir()
    (work / "orch_dashboard.py").write_text("raise SystemExit('shadow imported')\n")
    prefix = seen["argv"][: seen["argv"].index("-m")]  # [python, -P]: the launcher's own interpreter flags
    monkeypatch.undo()  # the real Popen again
    r = real_run(
        [*prefix, "-c", "import orch_dashboard; print(orch_dashboard.__file__)"],
        cwd=work, env=seen["env"], capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert str(work) not in r.stdout
