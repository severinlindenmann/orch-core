import json
import os
import subprocess

import pytest

from orch.errors import UsageError, ValidationError
from conftest import init_repo


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    init_repo(path, "main")
    (path / "README.md").write_text("hub\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def multi(ws_root, configure, agent):
    """A multi-repo workspace: a plain root folder with harness files and a sibling repo `hub`."""
    from orch.core.ops import Ops
    make_repo(ws_root / "hub")
    (ws_root / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (ws_root / ".claude" / "skills" / "deploy" / "SKILL.md").write_text("x", encoding="utf-8")
    (ws_root / ".claude" / "settings.json").write_text('{"permissions": {}}', encoding="utf-8")
    ws = configure(git={"repos": {"hub": {}}})
    ops = Ops(ws, agent)
    return ws, ops, ops.new("Back up config").id


def test_add_creates_branch_worktree_links_and_harness_files(multi):
    from orch.core import factory_runner, store, worktrees
    from orch.core.workspace import Workspace
    ws, ops, tid = multi
    r = worktrees.add(ops, tid, "hub")
    rel = ".claude/worktrees/hub/L-0001-back-up-config"
    assert r["branch"] == "feature/L-0001-back-up-config" and r["worktree"] == rel and r["new_branch"]
    wt = ws.root / rel
    assert git(wt, "branch", "--show-current") == "feature/L-0001-back-up-config"
    t = store.load(ws, tid)[1]
    assert t.meta["repos"] == ["hub"] and t.meta["branches"] == {"hub": r["branch"]}
    assert t.meta["worktrees"] == {"hub": rel}
    # a real .claude folder of per-entry links, never the whole .claude (which holds the worktrees: a loop)
    assert (wt / ".claude").is_dir() and not (wt / ".claude").is_symlink()
    assert sorted(r["harness"]) == [".claude/settings.json", ".claude/skills"]
    assert (wt / ".claude" / "skills").is_symlink() and (wt / ".claude" / "skills" / "deploy" / "SKILL.md").is_file()
    assert os.path.realpath(wt / ".claude" / "settings.json") == os.path.realpath(ws.root / ".claude" / "settings.json")
    assert not os.path.lexists(wt / ".claude" / "worktrees")
    assert git(wt, "status", "--porcelain") == ""  # the links are excluded locally
    # orch finds the workspace from inside the worktree, and the factory runner starts there
    assert Workspace.open(wt).home == ws.home
    assert factory_runner.start_dir(ws, t) == str(wt.resolve())


def test_base_and_an_existing_branch(multi):
    from orch.core import worktrees
    ws, ops, tid = multi
    hub = ws.root / "hub"
    git(hub, "checkout", "-q", "-b", "develop")
    (hub / "dev.txt").write_text("d", encoding="utf-8")
    git(hub, "add", "dev.txt")
    git(hub, "commit", "-q", "-m", "dev")
    git(hub, "checkout", "-q", "main")
    r = worktrees.add(ops, tid, "hub", base="develop")
    assert git(ws.root / r["worktree"], "rev-parse", "HEAD") == git(hub, "rev-parse", "develop")
    worktrees.remove(ops, tid, "hub")
    # the branch survived the removal: the next add checks it out again, and --base no longer applies
    with pytest.raises(UsageError, match="already exists"):
        worktrees.add(ops, tid, "hub", base="main")
    again = worktrees.add(ops, tid, "hub")
    assert not again["new_branch"] and (ws.root / again["worktree"] / "dev.txt").is_file()


def test_add_refusals(multi, ws_root):
    from orch.core import worktrees
    ws, ops, tid = multi
    with pytest.raises(UsageError, match="unknown repo"):
        worktrees.add(ops, tid, "nope")
    worktrees.add(ops, tid, "hub")
    with pytest.raises(UsageError, match="already has a worktree"):
        worktrees.add(ops, tid, "hub")
    (ws_root / "plain").mkdir()
    from orch.core.workspace import Workspace
    from orch.core.ops import Ops
    cfg = json.loads((ws.home / "config.json").read_text(encoding="utf-8"))
    cfg["git"]["repos"]["plain"] = {}
    (ws.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    ops2 = Ops(Workspace.open(ws_root), ops.actor)
    with pytest.raises(UsageError, match="not a git checkout"):
        worktrees.add(ops2, tid, "plain")
    cfg["git"]["branch_pattern"] = "feature/{key}..{slug}"
    (ws.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    ops3 = Ops(Workspace.open(ws_root), ops.actor)
    with pytest.raises(ValidationError, match="invalid branch name"):
        worktrees.add(ops3, ops3.new("Other").id, "hub")


def test_only_the_configured_harness_gets_files(ws_root, configure, agent):
    from orch.core import worktrees
    from orch.core.ops import Ops
    make_repo(ws_root / "hub")
    (ws_root / ".claude" / "skills").mkdir(parents=True)
    ws = configure(harnesses=["copilot"], git={"repos": {"hub": {}}})
    ops = Ops(ws, agent)
    r = worktrees.add(ops, ops.new("Docs").id, "hub")
    assert r["harness"] == [] and not os.path.lexists(ws.root / r["worktree"] / ".claude")


def test_remove_refuses_changes_then_removes_the_worktree_and_its_link(multi):
    from orch.core import store, worktrees
    ws, ops, tid = multi
    r = worktrees.add(ops, tid, "hub")
    wt = ws.root / r["worktree"]
    (wt / "new.txt").write_text("wip", encoding="utf-8")
    with pytest.raises(ValidationError, match="uncommitted changes"):
        worktrees.remove(ops, tid, "hub")
    assert wt.is_dir()
    (wt / "new.txt").unlink()
    worktrees.remove(ops, tid, "hub")
    assert not wt.exists() and not (ws.root / ".claude" / "worktrees").exists()
    assert (ws.root / ".claude" / "skills" / "deploy" / "SKILL.md").is_file()  # the workspace's own files stay
    t = store.load(ws, tid)[1]
    assert t.meta["worktrees"] == {} and t.meta["branches"] == {"hub": r["branch"]} and t.meta["repos"] == ["hub"]
    assert git(ws.root / "hub", "branch", "--list", r["branch"])
    with pytest.raises(UsageError, match="has no worktree"):
        worktrees.remove(ops, tid, "hub")


def test_remove_touches_only_worktrees_orch_placed(multi):
    from orch.core import worktrees
    ws, ops, tid = multi
    ops.link(tid, repo="hub", worktree="hub")
    with pytest.raises(UsageError, match="only worktrees it placed"):
        worktrees.remove(ops, tid, "hub")
    assert (ws.root / "hub" / "README.md").is_file()


def test_single_repo_workspace_keeps_its_worktrees_out_of_git_status(ws_root, configure, agent):
    from orch.core import worktrees
    from orch.core.ops import Ops
    make_repo(ws_root)
    git(ws_root, "add", "-A")
    git(ws_root, "commit", "-q", "-m", "orch")
    ws = configure()
    ops = Ops(ws, agent)
    r = worktrees.add(ops, ops.new("Fix it").id, "harness")
    assert r["worktree"] == ".claude/worktrees/harness/L-0001-fix-it"
    assert ".claude" not in git(ws_root, "status", "--porcelain", "--untracked-files=all")


def test_cli_add_and_remove_as_an_agent(multi, monkeypatch, capsys):
    from orch.cli import run
    ws, ops, tid = multi
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setenv("ORCH_SESSION", "s-1")
    assert run(["worktree", "add", tid, "--repo", "hub", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["worktree"] == ".claude/worktrees/hub/L-0001-back-up-config"
    assert run(["worktree", "remove", tid, "--repo", "hub"]) == 0
    assert "branch kept" in capsys.readouterr().out
    assert run(["worktree", "remove", tid, "--repo", "hub"]) == 2


def test_the_guard_lets_agents_manage_worktrees(ws):
    from orch.hooks.guard import evaluate
    for cmd in ("orch worktree add L-0001 --repo hub", "orch worktree remove L-0001 --repo hub"):
        assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow, cmd


def test_link_records_a_repo_on_its_own(aops, ws, monkeypatch, capsys):
    from orch.cli import run
    from orch.core import store
    tid = aops.new("Spans two repos").id
    aops.link(tid, repo="dlh_metadata")
    aops.link(tid, repo="dlh_metadata")  # once only
    t = store.load(ws, tid)[1]
    assert t.meta["repos"] == ["dlh_metadata"] and "linked repo dlh_metadata" in t.section("Log")
    assert t.section("Log").count("linked repo") == 1
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setenv("ORCH_SESSION", "s-1")
    assert run(["link", tid, "--repo", "hub"]) == 0
    assert store.load(ws, tid)[1].meta["repos"] == ["dlh_metadata", "hub"]
    capsys.readouterr()
    assert run(["link", tid]) == 2
    assert "nothing to link" in capsys.readouterr().err
    with pytest.raises(UsageError, match="nothing to link"):
        aops.link(tid)


def test_start_dir_takes_a_settings_link_to_the_workspaces_own_file_only(ws, tmp_path):
    from orch.core import factory_runner
    from orch.core.model import new_ticket
    (ws.root / ".claude").mkdir()
    (ws.root / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    wt = ws.root / ".claude" / "worktrees" / "hub" / "L-0001-x"
    (wt / ".claude").mkdir(parents=True)
    t = new_ticket("L-0001", "x", type="feature", priority="normal", size="m", created="2026-10-06")
    t.meta["worktrees"] = {"hub": ".claude/worktrees/hub/L-0001-x"}
    link = wt / ".claude" / "settings.json"
    link.symlink_to(ws.root / ".claude" / "settings.json")
    assert factory_runner.start_dir(ws, t) == str(wt.resolve())
    twin = tmp_path / "settings.json"
    twin.write_text("{}", encoding="utf-8")  # same bytes, another file an agent controls
    link.unlink()
    link.symlink_to(twin)
    assert factory_runner.start_dir(ws, t) is None
