"""Run-12a FB16: setup lets Claude Code trust the new folder (only that folder, only when asked)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from orch.setup import claude_folder
from tests.setup.fakes import FakePorts, names
from tests.setup.test_create import AGENT, fake_signer, run_ok  # noqa: F401 - fake_signer: signing stand-ins


def _home(tmp_path: Path, doc) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    if doc is not None:
        (home / ".claude.json").write_text(json.dumps(doc))
    return home, {"HOME": str(home), "PATH": os.environ.get("PATH", "")}


def test_trusts_only_the_new_folder_and_keeps_everything_else(tmp_path):
    home, env = _home(tmp_path, {"numStartups": 3, "projects": {"/other": {"hasTrustDialogAccepted": False, "x": 1}}})
    root = tmp_path / "work" / "ws"
    root.mkdir(parents=True)
    assert claude_folder.trust_folder(env, root) is True
    doc = json.loads((home / ".claude.json").read_text())
    assert doc["numStartups"] == 3 and doc["projects"]["/other"] == {"hasTrustDialogAccepted": False, "x": 1}
    assert doc["projects"][os.path.realpath(root)] == {"hasTrustDialogAccepted": True}


def test_no_config_file_or_a_broken_one_is_left_alone(tmp_path):
    root = tmp_path / "work" / "ws"
    root.mkdir(parents=True)
    _, env = _home(tmp_path, None)
    assert claude_folder.trust_folder(env, root) is False and not Path(env["HOME"], ".claude.json").exists()
    (Path(env["HOME"]) / ".claude.json").write_text("{not json")
    assert claude_folder.trust_folder(env, root) is False
    assert (Path(env["HOME"]) / ".claude.json").read_text() == "{not json"


def test_the_home_folder_itself_is_never_trusted(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    assert claude_folder.trust_folder(env, home) is False


def test_installed_check_follows_path(tmp_path):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert claude_folder.claude_installed({"PATH": str(exe.parent)}) is True
    assert claude_folder.claude_installed({"PATH": str(tmp_path)}) is False


def test_setup_runs_the_trust_step_only_when_the_profile_asks(tmp):
    f, ports, p, rv = run_ok(tmp, {**AGENT, "trust_folder": True})
    assert any(x["id"] == "trust" for x in rv["flags"]) and "trust_folder" in names(ports)
    assert {s["id"]: s["status"] for s in p["steps"]}["trust"] == "done"
    f, ports, p, rv = run_ok(tmp, AGENT)
    assert "trust_folder" not in names(ports)
