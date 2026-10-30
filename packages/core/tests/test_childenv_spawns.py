"""R5 review L4: our own dashboard spawns get the sanitized environment (no relative PYTHONPATH, no loader vars)."""

from __future__ import annotations

import subprocess

from orch.cli import remote as R
from orch.install import cli as I


def _capture(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


BAD = {
    "PATH": "/usr/bin",
    "HOME": "/tmp/h",
    "PYTHONPATH": "rel:/abs",
    "DYLD_INSERT_LIBRARIES": "/x.dylib",
    "LD_PRELOAD": "/y.so",
}


def test_remote_run_sanitizes_the_env(monkeypatch):
    seen = _capture(monkeypatch)
    R._run(["orch-dashboard", "up"], BAD)
    env = seen["env"]
    assert "DYLD_INSERT_LIBRARIES" not in env and "LD_PRELOAD" not in env
    assert "rel" not in env.get("PYTHONPATH", "").split(":")


def test_mint_login_sanitizes_the_env(monkeypatch):
    seen = _capture(monkeypatch)
    I._mint_login(["orch-dashboard"], BAD)
    env = seen["env"]
    assert "DYLD_INSERT_LIBRARIES" not in env and "LD_PRELOAD" not in env
    assert "rel" not in env.get("PYTHONPATH", "").split(":")
