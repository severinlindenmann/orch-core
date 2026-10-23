"""#1066: a Claude Code agent started in the workspace or a ticket work folder gets no trust or import prompt, and the
trust entries cover only workspace paths."""

from __future__ import annotations

import json
import os

import pytest

from orch.host.launch import LOGIN_EXEC
from orch.runner.claude_trust import TRUST_KEYS, pretrust, trust_paths
from orch.runner.config import RunnerConfig
from orch.runner.link import spawn_spec
from tests.runner.test_spawn_grants import GID, ME, SECRET, WS, agent, store_with

CLAUDE = ["$SHELL", "-l", "-c", LOGIN_EXEC, "claude", "Run orch status, then work on ticket K-1."]


@pytest.fixture
def ws(tmp_path):
    home, root = tmp_path / "home", tmp_path / "ws"
    (root / ".orch-work" / "K-1").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    home.mkdir()
    return home, root.resolve(), (root / ".orch-work" / "K-1").resolve()


def entries(home):
    return json.loads((home / ".claude.json").read_text())["projects"]


def test_the_root_and_the_work_folder_are_trusted_and_nothing_else(ws):
    home, root, work = ws
    (home / ".claude.json").write_text(json.dumps({"numStartups": 3, "projects": {"/x": {"keep": 1}}}))
    done = pretrust({"HOME": str(home)}, str(root), str(work))
    assert sorted(done) == sorted({str(root), str(work)})
    got = entries(home)
    assert set(got) == {"/x", str(root), str(work)}
    assert got["/x"] == {"keep": 1}
    assert all(got[p][k] is True for p in (str(root), str(work)) for k in TRUST_KEYS)
    assert json.loads((home / ".claude.json").read_text())["numStartups"] == 3
    assert pretrust({"HOME": str(home)}, str(root), str(work)) == []  # already there: nothing rewritten


def test_a_folder_outside_the_workspace_gets_no_entry(ws, tmp_path):
    home, root, work = ws
    for cwd in (tmp_path / "other", tmp_path, home, root.parent, root / "sub"):
        assert trust_paths(str(root), str(cwd)) == []
        assert pretrust({"HOME": str(home)}, str(root), str(cwd)) == []
    assert trust_paths("/", "/") == []
    assert trust_paths(str(root), str(root / ".orch-work" / ".." / "..")) == []  # dot-dot does not escape
    assert not (home / ".claude.json").exists()


def test_a_linked_or_unreadable_config_is_left_alone(ws, tmp_path):
    home, root, work = ws
    real = tmp_path / "real.json"
    real.write_text("{}")
    os.symlink(real, home / ".claude.json")
    assert pretrust({"HOME": str(home)}, str(root), str(work)) == []
    assert real.read_text() == "{}"
    os.unlink(home / ".claude.json")
    (home / ".claude.json").write_text("not json")
    assert pretrust({"HOME": str(home)}, str(root), str(work)) == []
    assert (home / ".claude.json").read_text() == "not json"


def test_claude_config_dir_is_honoured(ws, tmp_path):
    home, root, work = ws
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    pretrust({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg)}, str(root), str(work))
    assert (cfg / ".claude.json").exists() and not (home / ".claude.json").exists()


def launch(ws, argv, cwd=None, root=True):
    home, wroot, work = ws
    fixed = {"ORCH_HARNESS": "claude-code", "ORCH_TICKET": "K-1"}
    if root:
        fixed["ORCH_WORKSPACE"] = str(wroot)
    a = agent(argv=argv, env_fixed=fixed, cwd={"path": str(cwd or work), "dev": 1, "ino": 2})
    return spawn_spec(a, {"HOME": str(home), "SHELL": "/bin/sh", "PATH": "/usr/bin:/bin"}, RunnerConfig(),
                      store_with((WS, ME, GID, SECRET)))  # fmt: skip


def test_starting_a_claude_agent_records_the_trust(ws):
    home, root, work = ws
    launch(ws, CLAUDE)
    assert set(entries(home)) == {str(root), str(work)}


def test_another_harness_or_a_foreign_cwd_records_nothing(ws, tmp_path):
    home, root, work = ws
    launch(ws, [*CLAUDE[:4], "codex", "go"])
    launch(ws, CLAUDE, cwd=tmp_path / "other")
    launch(ws, CLAUDE, root=False)
    assert not (home / ".claude.json").exists()
