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
from test_factory_release import (Fake as RecipeFake, _g, _recipe, _refine, _not_stopping, bin_dir,  # noqa: F401
                                  remote)


def _msg(cid):
    return ["-m", f"{cid} work", "-m", "What: work", "-m", "Why: the ticket", "-m", "Risk: low"]
from test_factory_runner import Fake, _behavior, _payload

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
# a commit the gate allows: its message fits the workspace format ({C}: the bound child, filled in by _both)
COMMIT = 'git commit -m "{C} work" -m "What: y" -m "Why: z" -m "Risk: low"'
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
    _g(ws_root, "push", "-q", str(remote), "main")  # the base the release compares with
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
    p = {**_payload(b["session"], command.replace("{C}", str(b.get("child")))), "cwd": str(cwd)}
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
    _g(clone, "commit", "-q", *_msg(cid))
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
    fs.end(fws, run["b"]["session"])
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
    ("gitfile", ".git is a link or not a folder"),
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
    fws.config["git"]["agent_may"]["commit"] = True
    form = factory_runner.commit_form(fws, "L-0002")
    assert form
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
    fa.set_section(cid, "Verification", "- AC1: ran `pytest -q` on the branch, 3 passed")
    fa.move(cid, "testing")


def test_the_release_fetches_the_childs_commit_from_its_clone(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    (clone / "elephants.json").write_text("[]\n", encoding="utf-8")
    _g(clone, "add", "elephants.json")
    _g(clone, "commit", "-q", *_msg(cid))
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
    _g(clone, "commit", "-q", *_msg(cid))
    _to_testing(fa, cid, close_tasks)
    fake = RecipeFake()
    fr.tick(fws, human, fake)
    from orch.core import factory_report
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["sensitive"]
    assert not fake.calls


def _committed_clone(fws, fa, fh, human, remote, release="merge"):
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release=release)
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    (clone / "x.json").write_text("[]\n", encoding="utf-8")
    _g(clone, "add", "x.json")
    _g(clone, "commit", "-q", *_msg(cid))
    return eid, d, cid, clone


def test_a_child_without_commits_of_its_own_is_refused_and_never_closes(fws, fa, fh, human, close_tasks,
                                                                         remote, monkeypatch):  # noqa: F811
    from orch.core import factory_built, factory_close
    monkeypatch.setattr(factory_built, "move_refusal", lambda ws, t: None)  # past the move's precheck
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)  # the worker never commits: its work stays uncommitted in the clone
    (clone / "x.json").write_text("[]\n", encoding="utf-8")
    _to_testing(fa, cid, close_tasks)
    fake = RecipeFake()
    lines = fr.tick(fws, human, fake)
    assert any("the child's branch has no commits of its own" in x for x in lines), lines
    assert not fake.calls and fr.status(fws, store.load(fws, eid)[1], d)["stages"][0]["state"] == "failed"
    assert factory_close.tick(fws, human) == [] and store.load(fws, eid)[1].status == "open"


@pytest.mark.parametrize("where", ["app.txt", "orchestrator/note.md"])
def test_an_unpushed_workspace_commit_never_rides_along_in_a_clone(fws, fa, fh, human, close_tasks, remote,
                                                                    where):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    pushed = _g(fws.root, "rev-parse", "main")
    (fws.root / where).write_text("local only\n", encoding="utf-8")  # the human's work on main, not pushed yet
    tame = ["git", "-c", "filter.evil.process=", "-c", "filter.evil.clean=cat", "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false", "-c", "user.name=t", "-c", "user.email=t@x.invalid"]  # the hostile config
    subprocess.run([*tame, "add", "-f", where], cwd=fws.root, check=True)
    subprocess.run([*tame, "commit", "-q", "-m", "local only"], cwd=fws.root, check=True)
    assert _g(fws.root, "rev-parse", "main") != pushed
    clone, why = fc.ensure(fws, human, cid)
    assert why == "" and _g(clone, "rev-parse", "HEAD") == pushed == _g(remote, "rev-parse", "main")
    (clone / "x.json").write_text("[]\n", encoding="utf-8")
    _g(clone, "add", "x.json")
    _g(clone, "commit", "-q", *_msg(cid))
    assert _g(clone, "log", "--format=%s", f"{pushed}..HEAD").splitlines() == [f"{cid} work"]  # its own commit only
    _to_testing(fa, cid, close_tasks)
    fake = RecipeFake()
    lines = fr.tick(fws, human, fake)
    assert fr.status(fws, store.load(fws, eid)[1], d)["stages"][0]["state"] == "proven", lines
    assert sorted(os.listdir(fws.marks)) == []


def test_a_workspace_base_with_no_history_in_common_with_the_remote_gets_no_clone(fws, fa, fh, human, remote,
                                                                                   tmp_path):  # noqa: F811
    other = tmp_path / "other"
    other.mkdir()
    _g(other, "init", "-q", "-b", "main")
    _g(other, "commit", "-q", "--allow-empty", "-m", "unrelated")
    _g(other, "push", "-q", "--force", str(remote), "main")
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, why = fc.ensure(fws, human, cid)
    assert clone is None and "share no history" in why and fc.record(fws, cid) is None
    assert fc.failure(fws, cid)["why"] == why


@pytest.mark.parametrize("how", ["damaged", "removed", "cleaned"])
def test_a_missing_clone_record_never_falls_back_to_the_ticket_branch(fws, fa, fh, human, close_tasks, remote,
                                                                       how):  # noqa: F811
    eid, d, cid, clone = _committed_clone(fws, fa, fh, human, remote)
    _to_testing(fa, cid, close_tasks)
    if how == "damaged":
        fc._rec_path(fws, cid).write_text("{damaged", encoding="utf-8")
    elif how == "removed":
        fc._rec_path(fws, cid).unlink()
    else:
        assert fc.clean(fws, human, cid)
    other = f"feat/{cid.lower()}-other"  # an agent names a branch of the workspace on its ticket
    _g(fws.root, "branch", other)
    fa.link(cid, repo="app", branch=other)
    assert fr.child_source(fws, store.load(fws, cid)[1]) == (None, None,
                                                              f"the runner's clone record of {cid} is missing")
    fake = RecipeFake()
    lines = fr.tick(fws, human, fake)
    assert not fake.calls and any("clone record" in x for x in lines), lines


def test_a_child_that_never_had_a_clone_in_a_clonable_workspace_is_not_released(fws, fa, fh, human, close_tasks,
                                                                                   remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    _g(fws.root, "branch", f"feat/{cid.lower()}-work")
    fa.link(cid, repo="app", branch=f"feat/{cid.lower()}-work")  # a ticket field, no worktree of its own
    assert fr.child_source(fws, store.load(fws, cid)[1])[2] == f"the runner's clone record of {cid} is missing"


def test_each_fetch_from_a_clone_writes_its_config_again_under_its_lock(fws, fa, fh, human, close_tasks, remote,
                                                                       monkeypatch):  # noqa: F811
    eid, d, cid, clone = _committed_clone(fws, fa, fh, human, remote)
    _to_testing(fa, cid, close_tasks)
    rec = fc.record(fws, cid)
    cfg = clone / ".git" / "config"
    cfg.write_text(cfg.read_text(encoding="utf-8") + f"[uploadpack]\n\tpackObjectsHook = touch {fws.marks}/pack\n",
                   encoding="utf-8")
    fr.ensure_repo(fws, fr.load(fws)[0])
    branch, src, _ = fr.child_source(fws, store.load(fws, cid)[1])
    sha = _g(clone, "rev-parse", "HEAD")
    assert fr.fetch_child(fws, fr.load(fws)[0], branch, src, cid) == sha
    assert cfg.read_text(encoding="utf-8") == fc.config_text(rec["source"], fc._case_insensitive())
    assert fr.fetch_child(fws, fr.load(fws)[0], branch, src) is None  # from a clone only for its own child
    monkeypatch.setattr(fc, "LOCK_TIMEOUT", 0.1)
    with fc._clone_lock(fws, cid):  # `clean` (or a launch) holds the clone: the release does not fetch
        assert fr.fetch_child(fws, fr.load(fws)[0], branch, src, cid) is None
    assert sorted(os.listdir(fws.marks)) == []


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


def test_cli_clones_trust_says_which_clone_folders_claude_still_asks_for(fws, run, capsys, monkeypatch, tmp_path):
    from orch import actor as orch_actor
    from orch.cli import run as cli_run
    cid = run["cid"]
    start = str(fc.start_in(fws, fc.record(fws, cid)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir()
    cfg = tmp_path / "claude" / ".claude.json"
    cfg.write_text(json.dumps({"projects": {str(fc.root()): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setattr(orch_actor, "is_interactive", lambda: False)
    assert cli_run(["factory", "clones", "trust"]) != 0  # human only
    monkeypatch.delenv("ORCH_HARNESS", raising=False)
    monkeypatch.setattr(orch_actor, "is_interactive", lambda: True)
    capsys.readouterr()
    assert cli_run(["factory", "clones", "trust", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["clones"] == [{"child": cid, "path": start, "trusted": False}]  # the folder above does not count
    assert out["add"] == {start: {"hasTrustDialogAccepted": True}}
    cfg.write_text(json.dumps({"projects": {start: {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert cli_run(["factory", "clones", "trust"]) == 0 and "every clone folder is trusted" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", [
    "orch factory clones list", "orch factory clones clean L-0002", "orch factory clones trust", "uv run orch factory clones clean L-0002",
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



def test_the_clone_prompts_git_steps_pass_the_profile_and_the_commit_gate_one_by_one(fws, run):  # noqa: F811
    import re
    cid, clone, b = run["cid"], run["clone"], run["b"]
    prompt = factory_runner.factory_work_prompt(cid, factory_runner.commit_form(fws, cid),
                                                clone_tmp=str(fws.temporary_dir))
    assert "two separate plain commands, never chained" in prompt
    git = [s for s in re.findall(r"`([^`]+)`", prompt) if s.startswith("git ")]
    assert any(g.startswith("git add") for g in git) and any(g.startswith("git commit") for g in git)
    for g in git:
        cmd = g.replace("FILES", "x.json")
        assert "&&" not in cmd and ";" not in cmd, cmd
        assert dark_profile.match(fws, cmd) is not None, cmd  # git-basic runs each alone
        p = {**_payload(b["session"], cmd), "cwd": str(clone)}
        assert permits.bash_gate(fws, b, p) is None, cmd



@pytest.mark.parametrize("cmd", ['git add elephants.json && git commit -m "{cid} data" -m "What: x" -m "Why: z" -m "Risk: low"',
                                 'git add a.json; git commit -m "{cid} x" -m "What: x" -m "Why: z" -m "Risk: low"',
                                 'git add . && git commit -q -m "{cid} y" -m "What: x" -m "Why: z" -m "Risk: low"'])
def test_a_chained_git_add_and_commit_is_denied_with_how_to_run_them(fws, run, cmd):  # noqa: F811
    cid, clone, b = run["cid"], run["clone"], run["b"]
    cmd = cmd.replace("{cid}", cid)
    assert dark_profile.match(fws, cmd) is None  # a chain never matches
    guard, hook = _both(fws, b, cmd, clone)
    msg = (hook or {}).get("hookSpecificOutput", {}).get("decision", {}).get("message", "")
    assert _behavior(hook) == "deny" and "run git add and git commit as two separate commands" in msg, msg


def _evil_repo(path, marks, channel):
    """A repository whose own config (and attributes) try to run a program, as an agent could leave it in a clone:
    `channel` names what it plants (the channels measured with real git in the second review). Its files are dirty
    with a changed stat, so `git status` must read their content."""
    plain = {"PATH": "/usr/bin:/bin", "HOME": str(path.parent), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=plain)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    for args in (["add", "-A"], ["-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, env=plain)
    mark = f"sh -c 'touch {marks}/{channel}' #"
    attrs = "* filter=x diff=x\n"
    cfg = {"info-process": [("filter.x.process", mark)], "info-clean": [("filter.x.clean", mark),
                                                                         ("filter.x.smudge", mark)],
           "worktree-process": [("filter.x.process", mark)], "fsmonitor": [("core.fsmonitor", mark)],
           "ext": [("protocol.ext.allow", "always"), (f"url.ext::sh -c touch% {marks}/ext% .insteadOf", "SRC")],
           "hooks": []}[channel]
    if channel.startswith("info"):
        (path / ".git" / "info").mkdir(exist_ok=True)
        (path / ".git" / "info" / "attributes").write_text(attrs, encoding="utf-8")
    if channel == "worktree-process":
        (path / ".gitattributes").write_text(attrs, encoding="utf-8")
    if channel == "hooks":
        for h in ("post-checkout", "reference-transaction"):
            (path / ".git" / "hooks" / h).write_text(f"#!/bin/sh\ntouch {marks}/hooks\n", encoding="utf-8")
            (path / ".git" / "hooks" / h).chmod(0o755)
    for k, v in cfg:
        subprocess.run(["git", "-C", str(path), "config", k, v.replace("SRC", str(path.parent / "src"))],
                       check=True, env=plain)
    (path / "a.txt").write_text("two\n", encoding="utf-8")
    os.utime(path / "a.txt", (1, 1))


# measured with git 2.54 (Apple) before this change: info-process, info-clean (git status ran the filter: GIT_ATTR_SOURCE
# does not cover .git/info/attributes) and ext (the repository's protocol.ext.allow beat protocol.allow=never)
CHANNELS = ("info-process", "info-clean", "worktree-process", "fsmonitor", "ext", "hooks")


@pytest.mark.parametrize("channel", CHANNELS)
def test_no_program_a_clones_own_config_names_runs_in_the_runners_git_calls(fws, tmp_path_factory, monkeypatch,
                                                                            channel):
    """Every git call the runner makes in a clone (status and ls-files for the release check, rev-parse,
    merge-base, a fetch from a local repository, the checkout) runs pinned: its config is the runner's, written right
    before git starts, so nothing an agent planted in the clone's `.git` runs."""
    git = shutil.which("git")
    tmp_path = tmp_path_factory.mktemp("chan")
    src, repo, marks = tmp_path / "src", tmp_path / "C" / "repo", tmp_path / "marks"
    marks.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "f").write_text("f\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "i"],
                   check=True)
    repo.parent.mkdir()
    _evil_repo(repo, marks, channel)
    pin = {"child": "C", "inode": fc._ino(os.stat(repo)), "git_inode": fc._ino(os.stat(repo / ".git")),
           "source": str(src)}
    monkeypatch.setattr(fc, "_open_clone", lambda ws, child, rec: (os.open(repo, os.O_RDONLY | os.O_DIRECTORY),
                                                                   os.open(repo / ".git", os.O_RDONLY | os.O_DIRECTORY)))
    for args in (["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"],
                 ["ls-files", "-s", "-z"], ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
                 ["merge-base", "HEAD", "HEAD"],
                 ["fetch", "-q", "--no-tags", "--no-write-fetch-head", str(src), "+refs/heads/main:refs/x/main"],
                 ["checkout", "--quiet", "--no-recurse-submodules", "-b", "child", "HEAD"]):
        r = fc._git(git, fws, "-C", str(repo), *args, cwd=str(repo), timeout=30, pin=pin)
        assert r["code"] in (0, 1), (args, r)
        assert os.listdir(marks) == [], (channel, args)


def test_a_clone_folder_swapped_after_its_check_is_never_run_in(fws, tmp_path_factory):
    """A folder that is not the pinned one (another inode) gets no git call at all, and a path in the clones folder
    with no record of the runner's is refused."""
    git = shutil.which("git")
    tmp_path = tmp_path_factory.mktemp("swap")
    marks = tmp_path / "marks"
    marks.mkdir()
    repo = tmp_path / "C" / "repo"
    repo.parent.mkdir()
    _evil_repo(repo, marks, "worktree-process")
    pin = {"child": "C", "inode": "0:1", "git_inode": "0:2", "source": "/x"}
    r = fc._git(git, fws, "-C", str(repo), "status", cwd=str(repo), timeout=30, pin=pin)
    assert r["code"] is None and os.listdir(marks) == []
    r = fc._git(git, fws, "-C", str(fc.clone_dir(fws, "L-0099")), "status", cwd="/", timeout=30)
    assert r["code"] is None and "not a clone the runner recorded" in r["err"]


def test_a_tombstone_swapped_after_its_check_is_not_removed(tmp_path):
    keep = tmp_path / "other"
    keep.mkdir()
    (keep / "precious").write_text("x", encoding="utf-8")
    pinned = fc._ino(os.stat(tmp_path))  # some other folder's inode: what the runner pinned
    pfd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(OSError, match="not the pinned folder"):
            fc._rmtree_fd(pfd, "other", os.stat(keep).st_dev, pinned)
    finally:
        os.close(pfd)
    assert (keep / "precious").exists()
