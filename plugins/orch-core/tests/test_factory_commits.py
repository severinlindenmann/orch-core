"""AI Factory: where a runner-bound session may commit. One rule (factory_runner.own_work_tree) decides where a child's
session starts in a worktree, whether its prompt tells it to commit, and whether the guard (every permission mode) and
the permission hook (a second layer) let a commit run: only in the child's own linked git worktree, on a branch that
names the child and is not a default branch. Real git repositories on disk; no agent, no tmux."""
import json
import subprocess

import pytest

from orch.core import dark_profile, factory_runner, factory_sessions as fs, permits, store
from orch.hooks.guard import evaluate
from test_factory_runner import _behavior, _payload, _tick, _trusted_programs, fake  # noqa: F401
from test_factory_planner import _child, _epic

COMMIT = 'git commit -m "x" -m "What: y"'


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def repo(configure, human):
    """A Dark factory workspace that is a git repository on main (agents may commit), with git-basic in its profile."""
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": True}, git={"agent_may": {"commit": True}})
    _git("init", "-q", "-b", "main", cwd=ws.root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=ws.root)
    Ops(ws, human).set_factory_dark(True)
    dark_profile.add_baseline(ws, human, name="git-basic")
    return ws


def _worktree(ws, name, branch):
    _git("worktree", "add", "-q", "-b", branch, f".claude/worktrees/{name}", cwd=ws.root)
    return ws.root / ".claude" / "worktrees" / name


@pytest.fixture
def session(repo, agent, human, fake):  # noqa: F811
    """A child whose own worktree is .claude/worktrees/c, and the runner's session started in it."""
    from conftest import human_ops
    from orch.core.ops import Ops
    a, h = Ops(repo, agent), human_ops(repo, human)
    eid, d = _epic(repo, a, h, dark=True)
    cid = _child(a, eid)
    wt = _worktree(repo, "c", f"feat/{cid.lower()}")
    a.link(cid, repo="app", worktree=".claude/worktrees/c")
    _tick(repo, human, fake)
    b = next(x for x in fs.bindings(repo) if x["child"] == cid)
    assert b["start"] == str(wt.resolve())
    return {"ws": repo, "b": b, "wt": wt, "cid": cid, "eid": eid, "a": a, "h": h}


def _both(ws, b, command, cwd=None):
    """(the guard's answer, the permission hook's answer) for the bound session running `command` in `cwd`."""
    p = {**_payload(b["session"], command), **({"cwd": str(cwd)} if cwd is not None else {})}
    return evaluate(ws, p), permits.hook_decision(ws, p)


def test_its_own_worktree_commits_and_its_prompt_says_so(session, fake):  # noqa: F811
    ws, b, wt, cid = session["ws"], session["b"], session["wt"], session["cid"]
    prompt = next(a for n, c, a in fake.started if cid in n)[-1]
    assert "on this worktree's branch" in prompt and f'git commit -m "{cid} short summary"' in prompt
    for cwd in (wt, wt / "src"):
        (wt / "src").mkdir(exist_ok=True)
        guard, hook = _both(ws, b, COMMIT, cwd)
        assert guard.allow and _behavior(hook) == "allow", cwd
    p = {**_payload(b["session"], COMMIT)}
    p.pop("cwd")  # no working directory in the payload: refused by both gates
    assert not evaluate(ws, p).allow and _behavior(permits.hook_decision(ws, p)) == "deny"


@pytest.mark.parametrize("cmd", [
    "git -C .. commit -m x", "git --git-dir=../../.git commit -m x", "git --work-tree=.. commit -m x",
    "GIT_DIR=../../.git git commit -m x", "cd .. && git commit -m x", "pushd ..; git commit -m x",
])
def test_a_commit_pointed_elsewhere_is_refused(session, cmd):
    guard, hook = _both(session["ws"], session["b"], cmd, session["wt"])
    assert not guard.allow and "another folder" in guard.reason and _behavior(hook) == "deny"


@pytest.mark.parametrize("cmd", [
    "git commit -m x", "env git commit -m x", "GIT_DIR=x git commit", 'sh -c "git commit -m x"',
    "command git commit", "nice git commit", "timeout 5 git commit", "git -c alias.ci=commit ci", "git merge feat",
    "git cherry-pick abc", "git revert abc", "git am p.patch", "git rebase main", "git pull", "git commit-tree t",
    "git update-ref refs/heads/main abc", "git stash", '"g"it commit -m x', "g\\it commit", "Git commit -m x",
    "GIT commit",
])
def test_every_commit_form_is_gated(cmd):
    assert permits._git_commit(cmd), cmd


@pytest.mark.parametrize("cmd", ["git log --oneline", "git status", "git show HEAD", "git diff",
                                 "git branch --show-current"])
def test_reading_commands_pass_the_gate_anywhere_in_the_start_folder(session, cmd):
    ws, b = session["ws"], session["b"]
    assert permits.commit_refusal(ws, b, session["wt"], cmd) is None
    guard, hook = _both(ws, b, cmd, session["wt"])
    assert guard.allow, guard.reason


def test_a_command_without_git_is_not_gated():
    assert not permits._git_commit("orch log L-1 -m x") and not permits._git_commit("make test")


def test_the_shared_checkout_never_commits_whatever_its_branch(session, monkeypatch):
    ws, b = session["ws"], session["b"]
    _git("checkout", "-q", "-b", f"human-work-{session['cid'].lower()}", cwd=ws.root)  # not a default branch
    guard, hook = _both(ws, b, COMMIT, ws.root)
    assert not guard.allow and _behavior(hook) == "deny"  # outside the start folder
    root_b = {**b, "start": str(ws.root.resolve())}
    why = permits.commit_refusal(ws, root_b, ws.root, COMMIT)
    assert why and "below the workspace root" in why


@pytest.mark.parametrize("setup,why", [
    ("detached", "detached"), ("main", "default branch"), ("MAIN", "default branch"), ("develop-origin", "default"),
    ("upstream-trunk", "default"), ("reftable", "reftable"), ("invalid", "no real branch"), ("other-child", "name"),
    ("damaged-recipe", "recipe"), ("recipe-base", "default branch"), ("plain-folder", "linked git worktree"),
    ("nested-repo", "another git checkout"),
])
def test_the_allowlist_refuses_everything_else(session, setup, why, monkeypatch):
    ws, b, wt, cid = session["ws"], session["b"], session["wt"], session["cid"]
    gitdir = ws.root / ".git" / "worktrees" / "c"
    cwd = wt
    if setup == "detached":
        _git("checkout", "-q", "--detach", cwd=wt)
    elif setup in ("main", "MAIN", "develop-origin", "upstream-trunk", "other-child", "invalid", "recipe-base"):
        branch = {"main": "main", "MAIN": f"MAIN", "develop-origin": f"develop", "upstream-trunk": "trunk",
                  "other-child": "feat/l-9999", "invalid": ".invalid", "recipe-base": "release"}[setup]
        (gitdir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
        if setup == "develop-origin":
            (ws.root / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
            (ws.root / ".git" / "refs" / "remotes" / "origin" / "HEAD").write_text(
                "ref: refs/remotes/origin/develop\n", encoding="utf-8")
        if setup == "upstream-trunk":
            (ws.root / ".git" / "refs" / "remotes" / "upstream").mkdir(parents=True)
            (ws.root / ".git" / "refs" / "remotes" / "upstream" / "HEAD").write_text(
                "ref: refs/remotes/upstream/trunk\n", encoding="utf-8")
        if setup == "recipe-base":
            from orch.core import factory_release
            monkeypatch.setattr(factory_release, "load", lambda w: ({"base": "Release"}, None))
    elif setup == "reftable":
        (ws.root / ".git" / "reftable").mkdir()
    elif setup == "damaged-recipe":
        from orch.core import factory_release
        monkeypatch.setattr(factory_release, "load", lambda w: (None, "the release recipe cannot be read (X)"))
    elif setup == "plain-folder":
        (wt / ".git").unlink()
        (wt / ".git").mkdir()
    elif setup == "nested-repo":
        cwd = wt / "inner"
        cwd.mkdir()
        _git("init", "-q", "-b", f"feat/{cid.lower()}", cwd=cwd)
    guard, hook = _both(ws, b, COMMIT, cwd)
    assert not guard.allow and why in guard.reason, (setup, guard.reason)
    assert _behavior(hook) == "deny"


@pytest.mark.parametrize("mode", ["auto", "bypassPermissions", "acceptEdits"])
def test_the_guard_refuses_in_every_permission_mode(session, mode):
    """The guard answers PreToolUse whatever the mode (an allow rule, auto mode and bypass never reach the
    PermissionRequest hook): the commit on main is refused by the guard alone."""
    ws, b = session["ws"], session["b"]
    (ws.root / ".git" / "worktrees" / "c" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    p = {**_payload(b["session"], COMMIT), "cwd": str(session["wt"]), "permission_mode": mode}
    assert not evaluate(ws, p).allow


def test_a_binding_that_does_not_verify_is_refused_and_others_are_left_alone(session, monkeypatch):
    ws, b = session["ws"], session["b"]
    monkeypatch.setattr(fs, "chain_pids", lambda: {1})  # not under the recorded process
    assert not evaluate(ws, {**_payload(b["session"], COMMIT), "cwd": str(session["wt"])}).allow
    plain = {**_payload("22222222-3333-4444-8555-666666666666", COMMIT), "cwd": str(ws.root)}
    assert evaluate(ws, plain).allow  # not a factory session: as before
