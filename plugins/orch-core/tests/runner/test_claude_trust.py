"""#1066: a Claude Code agent started in the workspace or a ticket work folder gets no trust or import prompt, and the
trust entries cover only workspace paths."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orch.host.launch import LOGIN_EXEC
from orch.runner.claude_trust import TRUST_KEYS, pretrust, record_consent, trust_paths
from orch.runner.config import RunnerConfig
from orch.runner.link import spawn_spec
from tests.runner.test_spawn_grants import GID, ME, SECRET, WS, agent, store_with

CLAUDE = ["$SHELL", "-l", "-c", LOGIN_EXEC, "claude", "Run orch status, then work on ticket K-1."]


@pytest.fixture
def ws(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj" / "ws"
    (root / ".orch-work" / "K-1").mkdir(parents=True)
    (root / ".orch").mkdir()
    (tmp_path / "other").mkdir()
    home.mkdir()
    assert record_consent(root / ".orch")
    return home, root.resolve(), (root / ".orch-work" / "K-1").resolve()


def entries(home):
    return json.loads((home / ".claude.json").read_text())["projects"]


def pin(path):
    st = os.stat(path)
    return {"dev": st.st_dev, "ino": st.st_ino}


def test_only_the_ticket_work_folder_is_trusted_with_the_include_approval(ws):
    home, root, work = ws
    (home / ".claude.json").write_text(json.dumps({"numStartups": 3, "projects": {"/x": {"keep": 1}}}))
    done = pretrust({"HOME": str(home)}, str(root), str(work), pin(work))
    assert done == [str(work)]
    got = entries(home)
    assert set(got) == {"/x", str(work)}  # never the workspace root
    assert got["/x"] == {"keep": 1}
    assert all(got[str(work)][k] is True for k in TRUST_KEYS)
    assert json.loads((home / ".claude.json").read_text())["numStartups"] == 3
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []  # already there


def test_without_the_owners_consent_marker_or_a_store_nothing_is_written(ws):
    home, root, work = ws
    os.unlink(root / ".orch" / "claude-trust")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    os.rmdir(root / ".orch")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert not (home / ".claude.json").exists()


def test_a_marker_that_is_a_link_is_no_consent(ws, tmp_path):
    home, root, work = ws
    os.unlink(root / ".orch" / "claude-trust")
    (tmp_path / "m").write_text("x")
    os.symlink(tmp_path / "m", root / ".orch" / "claude-trust")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []


def test_a_folder_that_is_not_a_ticket_work_folder_gets_no_entry(ws, tmp_path):
    home, root, work = ws
    for cwd in (tmp_path / "other", tmp_path, home, root.parent, root, root / "sub", root / ".orch-work",
                work / "deep"):  # fmt: skip
        assert trust_paths(str(root), str(cwd)) == []
    assert trust_paths(str(root), str(root / ".orch-work" / ".." / "..")) == []
    assert not (home / ".claude.json").exists()


def test_the_home_folder_or_a_top_level_folder_is_never_a_root(tmp_path):
    home = tmp_path / "home"
    for root in (home, tmp_path, Path("/"), Path("/Users")):
        # even with a store, a marker and a work folder inside (the real ones made below tmp only)
        if str(root).startswith(str(tmp_path)):
            (root / ".orch").mkdir(parents=True, exist_ok=True)
            (root / ".orch-work" / "K-1").mkdir(parents=True, exist_ok=True)
            record_consent(root / ".orch")
        got = pretrust({"HOME": str(home)}, str(root), str(root / ".orch-work" / "K-1"))
        assert got == [], root
    assert not (home / ".claude.json").exists()
    # the claude config folder counts as home
    cfg = tmp_path / "cfgroot" / "x"
    cfg.mkdir(parents=True)
    (tmp_path / "cfgroot" / ".orch").mkdir()
    (tmp_path / "cfgroot" / ".orch-work" / "K-1").mkdir(parents=True)
    record_consent(tmp_path / "cfgroot" / ".orch")
    env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg)}
    assert pretrust(env, str(tmp_path / "cfgroot"), str(tmp_path / "cfgroot" / ".orch-work" / "K-1")) == []


def test_a_work_folder_that_is_a_link_out_of_the_workspace_is_rejected(ws, tmp_path):
    home, root, work = ws
    outside = tmp_path / "other"
    os.rmdir(work)
    os.symlink(outside, work)
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(outside)) == []
    assert not (home / ".claude.json").exists()
    # a linked .orch-work, too
    os.unlink(work)
    os.rmdir(root / ".orch-work")
    os.symlink(outside, root / ".orch-work")
    (outside / "K-1").mkdir()
    assert pretrust({"HOME": str(home)}, str(root), str(root / ".orch-work" / "K-1"), pin(outside / "K-1")) == []
    assert not (home / ".claude.json").exists()


def test_a_folder_that_is_not_the_pinned_one_gets_no_entry(ws):
    home, root, work = ws
    assert pretrust({"HOME": str(home)}, str(root), str(work), {"dev": 1, "ino": 2}) == []
    assert not (home / ".claude.json").exists()


def test_a_linked_or_unreadable_config_is_left_alone(ws, tmp_path):
    home, root, work = ws
    real = tmp_path / "real.json"
    real.write_text("{}")
    os.symlink(real, home / ".claude.json")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert real.read_text() == "{}"
    os.unlink(home / ".claude.json")
    (home / ".claude.json").write_text("not json")
    assert pretrust({"HOME": str(home)}, str(root), str(work), pin(work)) == []
    assert (home / ".claude.json").read_text() == "not json"


def test_claude_config_dir_is_honoured(ws, tmp_path):
    home, root, work = ws
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    pretrust({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg)}, str(root), str(work), pin(work))
    assert (cfg / ".claude.json").exists() and not (home / ".claude.json").exists()


def test_init_records_the_consent_marker_in_the_store(tmp_path):
    store = tmp_path / ".orch"
    assert not record_consent(store)  # no store folder: nothing made
    store.mkdir()
    assert record_consent(store) and (store / "claude-trust").is_file()
    assert not record_consent(tmp_path / "elsewhere")


def launch(ws, argv, cwd=None, root=True):
    home, wroot, work = ws
    fixed = {"ORCH_HARNESS": "claude-code", "ORCH_TICKET": "K-1"}
    if root:
        fixed["ORCH_WORKSPACE"] = str(wroot)
    a = agent(argv=argv, env_fixed=fixed, cwd={"path": str(cwd or work), **pin(cwd or work)})
    return spawn_spec(a, {"HOME": str(home), "SHELL": "/bin/sh", "PATH": "/usr/bin:/bin"}, RunnerConfig(),
                      store_with((WS, ME, GID, SECRET)))  # fmt: skip


def test_starting_a_claude_agent_records_the_trust(ws):
    home, root, work = ws
    launch(ws, CLAUDE)
    assert set(entries(home)) == {str(work)}


def test_another_harness_or_a_foreign_cwd_records_nothing(ws, tmp_path):
    home, root, work = ws
    launch(ws, [*CLAUDE[:4], "codex", "go"])
    launch(ws, CLAUDE, cwd=tmp_path / "other")
    launch(ws, CLAUDE, root=False)
    assert not (home / ".claude.json").exists()
