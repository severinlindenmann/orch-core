"""AI Factory: one separate clone per child, owned by the runner (docs/factory.md, "Per-child clones").

Real git in temporary folders (a workspace checkout with a hostile config, a bare remote): the clone is made with the
runner's isolation, the session starts in it with ORCH_HOME pointing at the workspace, its commits pass the generalised
commit gate only on the child's own branch, and the release fetches the child's branch from the clone. A fake launcher
stands in for tmux and a fake stands in for the recipe's commands: no agent, no tmux, no network."""
import json
import os
import shutil
import subprocess

import pytest

from orch.core import (dark_profile, epics, factory_clones as fc, factory_release as fr, factory_runner,
                       factory_sessions as fs, ledger, permits, store)
from orch.dashboard import launch
from orch.hooks.guard import evaluate
from test_factory_release import Fake as RecipeFake, _g, _recipe, _refine, _not_stopping, bin_dir, remote  # noqa: F401
from test_factory_runner import Fake, _behavior, _payload

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
COMMIT = 'git commit -m "x" -m "What: y"'
REAL_RESOLVE = factory_runner.resolve_bin


@pytest.fixture(autouse=True)
def _programs(monkeypatch):
    """git, and the recipe's stand-in programs, resolve for real (the runner's trust checks); claude, env, tmux and the
    rest under /opt/test; the user-scope settings pass."""
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: REAL_RESOLVE(name) if os.path.basename(name) in (
        "git", "gh", "make") else f"/opt/test/{os.path.basename(name)}")
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: None)


@pytest.fixture
def fws(configure, human, ws_root, remote):  # noqa: F811
    """A Dark factory workspace that lets agents commit, a git checkout on main (orchestrator/config.json committed),
    with a hostile repository config and hooks that leave a marker if anything runs them."""
    from orch.core.ops import Ops
    w = configure(factory={"enabled": True}, git={"agent_may": {"commit": True}})
    (ws_root / ".gitattributes").write_text("* filter=evil diff=evil\n", encoding="utf-8")
    (ws_root / "app.txt").write_text("hello\n", encoding="utf-8")
    _g(ws_root, "add", ".gitattributes", "app.txt", "orchestrator/config.json")
    _g(ws_root, "commit", "-q", "-m", "app")
    marks = ws_root.parent / "marks"
    marks.mkdir()
    hooks = ws_root.parent / "evil-hooks"
    hooks.mkdir()
    for h in ("post-checkout", "pre-commit", "post-commit", "reference-transaction", "pre-push"):
        for d in (hooks, ws_root / ".git" / "hooks"):
            (d / h).write_text(f"#!/bin/sh\ntouch {marks}/{h}\n", encoding="utf-8")
            (d / h).chmod(0o755)
    for k, v in {"core.hooksPath": str(hooks), "core.fsmonitor": f"touch {marks}/fsmonitor; false",
                 "filter.evil.smudge": f"touch {marks}/smudge; cat", "filter.evil.clean": f"touch {marks}/clean; cat",
                 "filter.evil.process": f"touch {marks}/process", "diff.evil.textconv": f"touch {marks}/textconv",
                 "alias.status": f"!touch {marks}/alias", "uploadpack.packObjectsHook": f"touch {marks}/pack",
                 "core.sshCommand": f"touch {marks}/ssh", "include.path": str(ws_root.parent / "inc.cfg")}.items():
        _g(ws_root, "config", k, v)
    (ws_root.parent / "inc.cfg").write_text(f"[alias]\n\tlog = !touch {marks}/include\n", encoding="utf-8")
    Ops(w, human).set_factory_dark(True)
    dark_profile.add_baseline(w, human, name="git-basic")
    w.marks = marks
    return w


@pytest.fixture
def fa(fws, agent):
    from orch.core.ops import Ops
    return Ops(fws, agent)


@pytest.fixture
def fh(fws, human):
    from conftest import human_ops
    return human_ops(fws, human)


def _epic(fa, fh, human, fws, release=None):
    e = fa.new("Elephants", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, **({"release": release} if release
                                                                                 else {})})
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    fs.arm(fws, human, d["id"])
    return e.id, d


def _child(fa, eid):
    c = fa.new("child", epic=eid)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    return c.id


def _tick(fws, human, fake):
    return factory_runner.tick(fws, human, fake, settings=launch.load_settings())


def _snapshot(gitdir):
    return sorted((str(p.relative_to(gitdir)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in gitdir.rglob("*") if p.is_file())


@pytest.fixture
def run(fws, fa, fh, human):
    """A Dark epic with one child, and one runner round that made its clone and started its session there."""
    eid, d = _epic(fa, fh, human, fws)
    cid = _child(fa, eid)
    before = _snapshot(fws.root / ".git")
    fake = Fake()
    lines = _tick(fws, human, fake)
    (b,) = [x for x in fs.bindings(fws) if x["child"] == cid]
    return {"eid": eid, "cid": cid, "d": d, "fake": fake, "lines": lines, "b": b, "before": before,
            "clone": fc.clone_dir(fws, cid)}


def _both(ws, b, command, cwd):
    p = {**_payload(b["session"], command), "cwd": str(cwd)}
    return evaluate(ws, p), permits.hook_decision(ws, p)


# -- making the clone ---------------------------------------------------------------------------------------------

def test_a_child_gets_its_own_clone_outside_the_workspace_and_starts_there(fws, run):
    cid, clone, (name, cwd, argv) = run["cid"], run["clone"], run["fake"].started[0]
    assert run["lines"] == [f"started {name}"] and cwd == str(clone.resolve()) == run["b"]["start"]
    assert fc.root() in clone.parents and fws.root not in clone.parents
    assert ledger.base_dir() not in clone.parents  # outside the orch config dir (the guard refuses everything there)
    assert _g(clone, "branch", "--show-current") == f"fx/{cid.lower()}"
    assert _g(clone, "rev-parse", "HEAD") == _g(fws.root, "rev-parse", "main")
    assert (clone / "app.txt").read_text(encoding="utf-8") == "hello\n"
    rec = fc.record(fws, cid)
    assert rec["path"] == str(clone) and rec["branch"] == f"fx/{cid.lower()}" and rec["base"] == "main"
    assert f"ORCH_HOME={fws.home.resolve()}" in argv
    prompt = argv[-1]
    assert "separate clone of the repository" in prompt and "Never push" in prompt
    assert f'git commit -m "{cid} short summary"' in prompt and str(fws.temporary_dir.resolve()) in prompt
    assert "orchestrator/temporary" not in prompt.replace(str(fws.temporary_dir.resolve()), "")


def test_the_workspace_config_hooks_and_attributes_never_run_and_never_reach_the_clone(fws, run):
    clone = run["clone"]
    _g(clone, "status")  # the clone's own config is what git reads there now
    assert sorted(os.listdir(fws.marks)) == []
    cfg = (clone / ".git" / "config").read_text(encoding="utf-8")
    for bad in ("evil", "alias", "include", "fsmonitor = touch", "sshCommand", "packObjectsHook", str(fws.marks)):
        assert bad not in cfg, bad
    assert "hooksPath = /dev/null" in cfg and "fsmonitor = false" in cfg and f"pushurl = {fc.NO_PUSH}" in cfg
    assert not (clone / ".git" / "hooks").exists() and not (clone / ".git" / "objects" / "info" / "alternates").exists()
    assert _snapshot(fws.root / ".git") == run["before"]  # the workspace repository was only read
    subprocess.run(["git", "status"], cwd=fws.root, capture_output=True)  # the markers are live in the workspace
    assert "fsmonitor" in os.listdir(fws.marks)


def test_a_relaunch_reuses_the_clone_keeps_its_work_and_writes_its_config_again(fws, run, human):
    cid, clone = run["cid"], run["clone"]
    (clone / "elephants.html").write_text("<p>hi</p>\n", encoding="utf-8")
    _g(clone, "add", "elephants.html")
    _g(clone, "commit", "-q", "-m", f"{cid} page")
    head = _g(clone, "rev-parse", "HEAD")
    with open(clone / ".git" / "config", "a", encoding="utf-8") as f:
        f.write("[alias]\n\tst = !touch x\n")
    path, why = fc.ensure(fws, human, cid)
    assert path == clone and why == ""
    assert _g(clone, "rev-parse", "HEAD") == head and "alias" not in (clone / ".git" / "config").read_text()
    assert factory_runner.start_dir(fws, store.load(fws, cid)[1]) == str(clone.resolve())


def test_a_folder_the_runner_did_not_record_is_never_touched(fws, fa, fh, human):
    eid, d = _epic(fa, fh, human, fws)
    cid = _child(fa, eid)
    dest = fc.clone_dir(fws, cid)
    dest.mkdir(parents=True)
    (dest / "mine.txt").write_text("keep\n", encoding="utf-8")
    fake = Fake()
    lines = _tick(fws, human, fake)
    assert not fake.started and any("has no record of it" in x for x in lines)
    assert (dest / "mine.txt").read_text(encoding="utf-8") == "keep\n" and fc.record(fws, cid) is None
    from orch.dashboard.data.factory import run_view
    r = run_view(fws, store.load(fws, eid)[1])
    assert r["state"] == "noclone" and r["clones"][0]["child"] == cid and "no record" in r["clones"][0]["why"]


@pytest.mark.parametrize("how,why", [
    ("missing-base", "checking out nobranch failed"), ("detached", "names no plain branch"),
    ("alternates", "borrows objects"), ("reftable", "reftable"), ("timeout", "did not finish within"),
    ("no-git", "git was not found"), ("damaged-recipe", "recipe cannot be loaded"),
])
def test_a_clone_that_cannot_be_made_starts_nothing_and_leaves_nothing(fws, fa, fh, human, monkeypatch, how, why):
    eid, d = _epic(fa, fh, human, fws)
    cid = _child(fa, eid)
    git = fws.root / ".git"
    if how == "missing-base":
        (git / "HEAD").write_text("ref: refs/heads/nobranch\n", encoding="utf-8")
    elif how == "detached":
        (git / "HEAD").write_text(_g(fws.root, "rev-parse", "HEAD") + "\n", encoding="utf-8")
    elif how == "alternates":
        (git / "objects" / "info" / "alternates").write_text("/tmp/elsewhere/objects\n", encoding="utf-8")
    elif how == "reftable":
        (git / "reftable").mkdir()
    elif how == "timeout":
        monkeypatch.setattr(fr, "run_command", lambda *a, **k: {"code": None, "out": "", "out_size": 0, "err": "",
                                                                 "timed_out": True})
    elif how == "no-git":
        monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: None if name == "git" else f"/opt/test/{name}")
    elif how == "damaged-recipe":
        monkeypatch.setattr(fr, "load", lambda w: (None, "the release recipe cannot be read (X)"))
    fake = Fake()
    lines = _tick(fws, human, fake)
    assert not fake.started and not fs.bindings(fws), lines
    assert any(why in x for x in lines), lines
    assert fc.record(fws, cid) is None and not fc.clone_dir(fws, cid).exists()
    assert why in fc.failure(fws, cid)["why"]


def test_the_runner_never_deletes_a_clone_and_a_human_can(fws, run, human, agent):
    from orch.errors import HumanOnlyError
    assert [c["child"] for c in fc.listing(fws)] == [run["cid"]]
    with pytest.raises(HumanOnlyError):
        fc.clean(fws, agent, run["cid"])
    assert fc.clean(fws, human, run["cid"]) and not run["clone"].exists() and fc.listing(fws) == []
    assert not fc.clean(fws, human, run["cid"])


def test_a_record_that_does_not_read_back_exactly_does_not_count(fws, run):
    p = fc._rec_path(fws, run["cid"])
    body = json.loads(p.read_text(encoding="utf-8"))
    for bad in ({"path": "/tmp/elsewhere"}, {"branch": "main"}, {"child": "L-9999"}, {"extra": "x"}):
        p.write_text(json.dumps({**body, **bad}), encoding="utf-8")
        assert fc.record(fws, run["cid"]) is None, bad
        assert fc.own_clone(fws, run["clone"], run["cid"])


# -- ORCH_HOME: orch and the hooks act on the workspace's own tickets ----------------------------------------------

def test_orch_in_the_clone_with_orch_home_reads_and_writes_the_workspace_tickets(fws, run, monkeypatch):
    from orch.cli import run as cli_run
    from orch.core.workspace import Workspace
    clone, cid = run["clone"], run["cid"]
    monkeypatch.chdir(clone)
    assert (clone / "orchestrator" / "config.json").is_file()  # the clone carries a copy of its own
    monkeypatch.setenv("ORCH_HOME", str(fws.home))
    w = Workspace.open(clone)
    assert w.root.resolve() == fws.root.resolve()
    ledger._checkouts.clear()
    assert ledger.checkout_id(w) == run["b"]["checkout"]  # the workspace's checkout, never the clone's
    assert cli_run(["log", cid, "-m", "from the clone"]) == 0
    assert "from the clone" in store.load(fws, cid)[1].section("Log")
    assert cli_run(["show", cid]) == 0
    monkeypatch.delenv("ORCH_HOME")
    ledger._checkouts.clear()
    assert Workspace.open(clone).root.resolve() == clone.resolve()  # without it: the clone's own copy


# -- the generalised commit gate ---------------------------------------------------------------------------------

def test_a_commit_in_its_own_clone_on_its_branch_passes_both_gates(fws, run):
    clone = run["clone"]
    (clone / "src").mkdir()
    for cwd in (clone, clone / "src"):
        guard, hook = _both(fws, run["b"], COMMIT, cwd)
        assert guard.allow and _behavior(hook) == "allow", cwd
    assert factory_runner.own_work_tree(fws, clone, run["cid"]) is None


@pytest.mark.parametrize("setup,why", [
    ("detached", "detached"), ("other-branch", "not the one the runner made"), ("default", "default branch"),
    ("reftable", "reftable"), ("damaged-recipe", "recipe"), ("no-record", "not a clone the runner made"),
    ("gitfile", "not a plain .git folder"),
])
def test_the_clone_gate_refuses_everything_else(fws, run, monkeypatch, setup, why):
    clone, cid = run["clone"], run["cid"]
    if setup == "detached":
        _g(clone, "checkout", "-q", "--detach")
    elif setup == "other-branch":
        _g(clone, "checkout", "-q", "-b", f"fx/{cid.lower()}-two")
    elif setup == "default":
        monkeypatch.setattr(fr, "load", lambda w: ({"base": f"FX/{cid}"}, None))
    elif setup == "reftable":
        (clone / ".git" / "reftable").mkdir()
    elif setup == "damaged-recipe":
        monkeypatch.setattr(fr, "load", lambda w: (None, "the release recipe cannot be read (X)"))
    elif setup == "no-record":
        fc._rec_path(fws, cid).unlink()
    elif setup == "gitfile":
        shutil.move(str(clone / ".git"), str(clone.parent / "moved.git"))
        (clone / ".git").write_text(f"gitdir: {clone.parent / 'moved.git'}\n", encoding="utf-8")
    guard, hook = _both(fws, run["b"], COMMIT, clone)
    assert not guard.allow and why in guard.reason, (setup, guard.reason)
    assert _behavior(hook) == "deny"


def test_another_childs_clone_is_not_this_childs(fws, fa, run, human):
    other = _child(fa, run["eid"])
    path, _ = fc.ensure(fws, human, other)
    guard, hook = _both(fws, run["b"], COMMIT, path)
    assert not guard.allow and "not inside the folder" in guard.reason and _behavior(hook) == "deny"
    assert "own clone" in factory_runner.own_work_tree(fws, path, run["cid"])
    moved = {**run["b"], "start": str(path)}  # a binding naming the other clone still names this child
    assert "own clone" in permits.commit_refusal(fws, moved, path, COMMIT)


# -- the guard keeps the records closed and the clone's work tree open --------------------------------------------

def test_the_guard_lets_the_session_work_in_its_clone_and_keeps_git_and_records_closed(fws, run):
    clone, sid = run["clone"], run["b"]["session"]
    base = ledger.base_dir()

    def ev(tool, inp):
        return evaluate(fws, {"session_id": sid, "tool_name": tool, "tool_input": inp, "cwd": str(clone)})
    assert ev("Write", {"file_path": str(clone / "elephants.html"), "content": "x"}).allow
    assert ev("Edit", {"file_path": str(clone / "app.txt"), "old_string": "a", "new_string": "b"}).allow
    for cmd in ("git status", "git add elephants.html", "git diff", "ls", "cat app.txt", f"orch show {run['cid']}"):
        assert ev("Bash", {"command": cmd}).allow, cmd
    assert not ev("Write", {"file_path": str(clone / ".git" / "config"), "content": "x"}).allow
    assert not ev("Bash", {"command": "echo x > .git/config"}).allow
    for cmd in (f"cat {base}/permits/child-clones/x.json", "ls ~/.config/orch/permits/child-clones",
                f"rm {fc._rec_path(fws, run['cid'])}"):
        assert not ev("Bash", {"command": cmd}).allow, cmd
        assert permits.never_grantable(fws, cmd), cmd
    assert not ev("Read", {"file_path": str(fc._rec_path(fws, run["cid"]))}).allow


def test_an_agent_cannot_make_or_remove_a_clone(fws, run, agent):
    from orch.errors import HumanOnlyError
    for call in (lambda: fc.ensure(fws, agent, run["cid"]), lambda: fc.clean(fws, agent, run["cid"])):
        with pytest.raises(HumanOnlyError):
            call()


def test_the_clone_prompt_names_the_same_orch_commands_as_the_worktree_prompt(fws):
    import re
    form = factory_runner.commit_form(fws, "L-0002")
    clone = factory_runner.factory_work_prompt("L-0002", form, clone_tmp="/abs/ws/orchestrator/temporary")
    tree = factory_runner.factory_work_prompt("L-0002", form)
    assert set(re.findall(r"`([^`]+)`", clone)) == set(re.findall(r"`([^`]+)`", tree))
    assert "\n" not in clone and "/abs/ws/orchestrator/temporary" in clone
    assert factory_runner.factory_work_prompt("L-0002", form, clone_tmp="relative/tmp") is None
    no = factory_runner.factory_work_prompt("L-0002", None, clone_tmp="/abs/tmp")
    assert "Do not commit" in no and "git commit" not in no


# -- the release takes the child's branch from its clone ---------------------------------------------------------

def _to_testing(fa, cid, close_tasks):
    fa.claim(cid)
    close_tasks(fa, cid)
    fa.set_section(cid, "Verification", "- AC1: ran the suite, green")
    fa.move(cid, "testing")


def test_the_release_fetches_the_childs_commit_from_its_clone(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    (clone / "elephants.json").write_text("[]\n", encoding="utf-8")
    _g(clone, "add", "elephants.json")
    _g(clone, "commit", "-q", "-m", f"{cid} data")
    sha = _g(clone, "rev-parse", "HEAD")
    _to_testing(fa, cid, close_tasks)
    assert fr.child_source(fws, store.load(fws, cid)[1]) == (f"fx/{cid.lower()}", clone, "")
    before = _snapshot(fws.root / ".git")
    fake = RecipeFake()
    lines = fr.tick(fws, human, fake)
    assert any("proven" in x for x in lines), lines
    merged = [a for a, *_ in fake.calls if "--match-head-commit" in a]
    assert merged and merged[0][merged[0].index("--match-head-commit") + 1] == sha
    assert _snapshot(fws.root / ".git") == before  # the workspace repository is never written by the release
    assert sorted(os.listdir(fws.marks)) == []


def test_a_sensitive_change_in_the_clone_stops_the_release(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    (clone / ".github").mkdir()
    (clone / ".github" / "ci.yml").write_text("evil\n", encoding="utf-8")
    _g(clone, "add", ".github/ci.yml")
    _g(clone, "commit", "-q", "-m", f"{cid} ci")
    _to_testing(fa, cid, close_tasks)
    fake = RecipeFake()
    fr.tick(fws, human, fake)
    from orch.core import factory_report
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["sensitive"]
    assert not fake.calls


# -- the human's list and clean -----------------------------------------------------------------------------------

def test_cli_list_and_clean_are_human_only_and_confirmed(fws, run, capsys, monkeypatch):
    from orch import actor as orch_actor
    from orch.cli import run as cli_run
    cid, clone = run["cid"], run["clone"]

    def agent():
        monkeypatch.setenv("ORCH_HARNESS", "test-agent")
        monkeypatch.setattr(orch_actor, "is_interactive", lambda: False)

    def human(confirm):
        monkeypatch.delenv("ORCH_HARNESS", raising=False)
        monkeypatch.setattr(orch_actor, "is_interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": confirm)
    agent()
    for args in (["list"], ["clean", cid]):
        assert cli_run(["factory", "clones", *args]) != 0
    human("x")
    capsys.readouterr()
    assert cli_run(["factory", "clones", "list"]) == 0 and str(clone) in capsys.readouterr().out
    human(cid)
    assert cli_run(["factory", "clones", "clean", cid]) != 0 and clone.exists()  # its session still runs
    fs.end(fws, run["b"]["session"])
    human("nope")
    assert cli_run(["factory", "clones", "clean", cid]) != 0 and clone.exists()
    human(cid)
    assert cli_run(["factory", "clones", "clean", cid]) == 0 and not clone.exists() and fc.record(fws, cid) is None


@pytest.mark.parametrize("cmd", [
    "orch factory clones list", "orch factory clones clean L-0002", "uv run orch factory clones clean L-0002",
    "python3 -c 'from orch.core import factory_clones'",
    "python3 -c \"from orch.cli import app; app(['factory', 'clones', 'clean', 'L-0002'])\"",
])
def test_guard_keeps_agents_from_the_clones_commands(fws, cmd):
    assert not evaluate(fws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow, cmd
    assert permits.never_grantable(fws, cmd), cmd


def test_a_clone_moved_off_its_branch_is_not_started_in_and_the_run_view_says_why(fws, run, human):
    from orch.dashboard.data.factory import run_view
    _g(run["clone"], "checkout", "-q", "-b", "elsewhere")
    run["fake"].stop(run["b"]["name"])
    fs.end(fws, run["b"]["session"], wake="")  # it ended; the next round may start it again
    lines = _tick(fws, human, run["fake"])
    assert len(run["fake"].started) == 1 and any("not its own work tree" in x for x in lines), lines
    r = run_view(fws, store.load(fws, run["eid"])[1])
    assert r["clones"] and "not the one the runner made" in r["clones"][0]["why"]


# -- links planted in the agent-writable clones folder ------------------------------------------------------------

@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    (d / "repo").mkdir(parents=True)
    (d / "repo" / "precious.txt").write_text("keep\n", encoding="utf-8")
    (d / "precious.txt").write_text("keep\n", encoding="utf-8")
    return d


def _intact(d):
    assert (d / "precious.txt").read_text(encoding="utf-8") == "keep\n"
    assert (d / "repo" / "precious.txt").read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("level", ["clones-root", "workspace", "child"])
def test_a_link_on_the_way_to_a_new_clone_makes_nothing_there(fws, fa, fh, human, outside, level):
    eid, d = _epic(fa, fh, human, fws)
    cid = _child(fa, eid)
    dest = fc.clone_dir(fws, cid)
    at = {"clones-root": fc.root(), "workspace": dest.parent.parent, "child": dest.parent}[level]
    at.parent.mkdir(parents=True, exist_ok=True)
    if at.exists():
        shutil.rmtree(at)
    at.symlink_to(outside, target_is_directory=True)
    path, why = fc.ensure(fws, human, cid)
    assert path is None and "is a link or not a folder" in why
    assert sorted(os.listdir(outside)) == ["precious.txt", "repo"] and fc.record(fws, cid) is None
    _intact(outside)


def test_clean_never_follows_a_link_inside_the_clone(fws, run, human, outside):
    (run["clone"] / "evil").symlink_to(outside, target_is_directory=True)
    (run["clone"] / "evil-file").symlink_to(outside / "precious.txt")
    fs.end(fws, run["b"]["session"])
    assert fc.clean(fws, human, run["cid"]) and not os.path.lexists(run["clone"])
    _intact(outside)


@pytest.mark.parametrize("swap", ["clone-is-link", "parent-is-link", "clones-root-is-link", "other-folder"])
def test_clean_reverifies_the_exact_clone_right_before_deleting(fws, run, human, outside, swap):
    from orch.errors import ValidationError
    clone = run["clone"]
    fs.end(fws, run["b"]["session"])
    if swap == "clone-is-link":
        shutil.move(str(clone), str(clone.parent / "moved"))
        clone.symlink_to(outside / "repo", target_is_directory=True)
    elif swap == "parent-is-link":
        shutil.move(str(clone.parent), str(clone.parent.parent / "moved"))
        clone.parent.symlink_to(outside, target_is_directory=True)
    elif swap == "clones-root-is-link":
        shutil.move(str(fc.root()), str(fc.root().parent / "moved-clones"))
        (outside / fc.clone_dir(fws, run["cid"]).relative_to(fc.root()).parent).mkdir(parents=True)
        (outside / fc.clone_dir(fws, run["cid"]).relative_to(fc.root())).mkdir()
        fc.root().symlink_to(outside, target_is_directory=True)
    else:  # a real folder of the same name, not the one the runner made
        shutil.move(str(clone), str(clone.parent / "moved"))
        clone.mkdir()
        (clone / "precious.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        fc.clean(fws, human, run["cid"])
    assert fc.record(fws, run["cid"]) is not None  # the record stays: nothing was removed
    _intact(outside)
    if swap == "other-folder":
        assert (clone / "precious.txt").exists()
    assert fc.own_clone(fws, clone, run["cid"])  # and it is not the child's own work tree any more


def test_a_swapped_clone_is_never_reused(fws, run, human):
    clone = run["clone"]
    shutil.move(str(clone), str(clone.parent / "moved"))
    shutil.copytree(clone.parent / "moved", clone, symlinks=True)  # same content, another inode
    path, why = fc.ensure(fws, human, run["cid"])
    assert path is None and "not the folder the runner made" in why
    assert "inode" in fc.own_clone(fws, clone, run["cid"])
