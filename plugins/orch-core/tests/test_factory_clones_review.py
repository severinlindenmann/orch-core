"""AI Factory per-child clones: regressions from the defensive review of the first build. Links and swaps raced in
while the runner works, the release re-checking the clone, the orch folder and harness files of a clone, the commit
gate's other spellings, commit messages, odd git dirs, removal by tombstone and the clone lock, one clone per round."""
import os
import shutil
import threading

import pytest

from orch.core import factory_clones as fc, factory_release as fr, factory_runner, factory_sessions as fs, permits
from orch.core import store
from orch.errors import UsageError, ValidationError
from orch.hooks.guard import evaluate
from test_factory_clones import (COMMIT, _both, _child, _epic, _msg, _programs, _snapshot, _tick, _to_testing,  # noqa
                                 fa, fh, fws, outside, run)
from test_factory_release import Fake as RecipeFake, _g, _not_stopping, _recipe, bin_dir, remote  # noqa: F401
from test_factory_runner import Fake, _behavior, _payload

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _swap_for_link(path, target):
    shutil.move(str(path), str(path.parent / f"{path.name}-moved"))
    path.symlink_to(target, target_is_directory=True)


# -- 1: the config and hooks are written by descriptor --------------------------------------------------------------

def test_a_git_dir_swapped_for_a_link_mid_reuse_writes_nothing_through_it(fws, run, human, outside, monkeypatch):
    (outside / "config").write_text("keep\n", encoding="utf-8")
    (outside / "hooks").mkdir()
    (outside / "hooks" / "post-commit").write_text("keep\n", encoding="utf-8")
    real = fc._odd_fd

    def swap(g):  # the .git descriptor is open; now an agent swaps the name for a link
        why = real(g)
        _swap_for_link(run["clone"] / ".git", outside)
        return why
    monkeypatch.setattr(fc, "_odd_fd", swap)
    fc.ensure(fws, human, run["cid"])
    assert (outside / "config").read_text(encoding="utf-8") == "keep\n"
    assert (outside / "hooks" / "post-commit").read_text(encoding="utf-8") == "keep\n"
    assert "hooksPath = /dev/null" in (run["clone"] / ".git-moved" / "config").read_text(encoding="utf-8")
    monkeypatch.setattr(fc, "_odd_fd", real)
    path, why = fc.ensure(fws, human, run["cid"])  # and the swapped clone is not used again
    assert path is None and "link or not a folder" in why


# -- 2+3: exclusive creation, early pinning, removing only what the attempt made ----------------------------------

def test_a_folder_made_by_another_party_before_the_clone_survives(fws, fa, fh, human, monkeypatch):
    eid, d = _epic(fa, fh, human, fws)
    cid = _child(fa, eid)
    dest = fc.clone_dir(fws, cid)
    real = fc.base_branch

    def racing(ws, src):  # another party makes the folder after the record check, before the clone
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "other-runners-work.txt").write_text("keep\n", encoding="utf-8")
        return real(ws, src)
    monkeypatch.setattr(fc, "base_branch", racing)
    path, why = fc.ensure(fws, human, cid)
    assert path is None and "no record of it" in why
    assert (dest / "other-runners-work.txt").read_text(encoding="utf-8") == "keep\n" and fc.record(fws, cid) is None


def test_a_real_folder_swapped_in_during_the_clone_is_refused_and_kept(fws, fa, fh, human, monkeypatch):
    eid, d = _epic(fa, fh, human, fws)
    cid = _child(fa, eid)
    dest = fc.clone_dir(fws, cid)
    real = fr.run_command

    def run(argv, cwd, env, timeout, **kw):
        r = real(argv, cwd, env, timeout, **kw)
        if "clone" in argv:
            shutil.move(str(dest), str(dest.parent / "made"))
            dest.mkdir()
            (dest / "theirs.txt").write_text("keep\n", encoding="utf-8")
        return r
    monkeypatch.setattr(fr, "run_command", run)
    path, why = fc.ensure(fws, human, cid)
    assert path is None and "replaced" in why and fc.record(fws, cid) is None
    assert (dest / "theirs.txt").read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("swap", ["git-copy", "git-link"])
def test_the_git_dir_is_pinned_too(fws, run, human, outside, swap):
    git = run["clone"] / ".git"
    if swap == "git-copy":
        shutil.copytree(git, run["clone"] / ".git-copy", symlinks=True)
        shutil.rmtree(git)
        (run["clone"] / ".git-copy").rename(git)
    else:
        _swap_for_link(git, outside)
    assert ".git" in fc.own_clone(fws, run["clone"], run["cid"])
    assert fc.verify(fws, run["cid"])[0] is None
    assert fc.ensure(fws, human, run["cid"])[0] is None


# -- 4: the release checks the clone before it fetches ------------------------------------------------------------

@pytest.mark.parametrize("swap", ["link", "copy"])
def test_the_release_never_fetches_from_a_swapped_clone(fws, run, tmp_path, remote, swap):  # noqa: F811
    clone, cid = run["clone"], run["cid"]
    if swap == "link":
        _swap_for_link(clone, tmp_path)
    else:
        shutil.move(str(clone), str(clone.parent / "moved"))
        shutil.copytree(clone.parent / "moved", clone, symlinks=True)
    t = store.load(fws, cid)[1]
    branch, src, why = fr.child_source(fws, t)
    assert branch is None and src is None and why
    rec = fr.check_recipe(_recipe(remote), fws, check_programs=False)
    found, hits, errors = fr.classify(fws, rec, [t])
    assert cid in errors and cid not in found


# -- 5 and 19: the clone's orch folder and harness files ---------------------------------------------------------

def test_a_child_commit_to_the_orch_folder_stops_the_release_whatever_the_recipe(fws, fa, fh, human, close_tasks,
                                                                                  remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote, sensitive_paths=[]))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    t = clone / "orchestrator" / "tickets" / "open" / f"{cid}-forged.md"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("---\nstatus: done\n---\n", encoding="utf-8")
    _g(clone, "add", str(t.relative_to(clone)))
    _g(clone, "commit", "-q", *_msg(cid))
    _to_testing(fa, cid, close_tasks)
    fake = RecipeFake()
    fr.tick(fws, human, fake)
    from orch.core import factory_report
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["sensitive"]
    assert not fake.calls and fr.always_sensitive(fws) == ["orchestrator"]


@pytest.mark.parametrize("rel,why", [
    ("orchestrator/tickets/open/L-0002-x.md", "copy of the orch folder"), ("orchestrator/config.json", "orch folder"),
    ("Orchestrator/.state/events.jsonl", "orch folder"), (".claude/settings.json", "harness settings"),
    (".claude/settings.local.json", "harness settings"), (".mcp.json", "harness settings"),
])
def test_file_tools_never_write_a_clones_orch_folder_or_harness_settings(fws, run, rel, why):
    p = run["clone"] / rel
    d = evaluate(fws, {"session_id": run["b"]["session"], "tool_name": "Write",
                       "tool_input": {"file_path": str(p), "content": "x"}, "cwd": str(run["clone"])})
    assert not d.allow and why in d.reason
    ok = evaluate(fws, {"session_id": run["b"]["session"], "tool_name": "Write",
                        "tool_input": {"file_path": str(run["clone"] / "orchestrator" / "notes.md"), "content": "x"},
                        "cwd": str(run["clone"])})
    assert ok.allow


# -- 6: a Dark answer only for a session in its own folder ----------------------------------------------------------

def test_a_dark_command_from_another_folder_is_denied(fws, run, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    for cwd, want in ((run["clone"], "allow"), (run["clone"] / "src", "allow"), (other, "deny"), (fws.root, "deny")):
        (run["clone"] / "src").mkdir(exist_ok=True)
        out = permits.hook_decision(fws, {**_payload(run["b"]["session"], "git status"), "cwd": str(cwd)})
        assert _behavior(out) == want, cwd


# -- 7: every other way to point git elsewhere or write another ref -----------------------------------------------

@pytest.mark.parametrize("cmd", [
    "GIT_COMMON_DIR={ws}/.git git update-ref refs/heads/main HEAD", "GIT_INDEX_FILE=x git commit -m x",
    "GIT_OBJECT_DIRECTORY={ws}/.git/objects git commit -m x", "git -c core.worktree=/tmp commit -m x",
    "git --config-env=core.worktree=X commit -m x", "git push {ws} HEAD:refs/heads/main", "git push origin main",
    "git push --all origin", "git branch -f main HEAD", "git branch -D other", "git branch newname",
    "git tag -f v1", "git tag v1", "git symbolic-ref HEAD refs/heads/main", "git update-ref refs/heads/fx/x HEAD",
    "git fetch origin main:main", "git replace a b", "git notes add -m x", "export GIT_DIR=x; git status",
    "env GIT_WORK_TREE=/tmp git add .",
])
def test_the_commit_gate_refuses_other_dirs_config_and_refs(fws, run, cmd):
    cmd = cmd.replace("{ws}", str(fws.root))
    assert permits._git_commit(cmd), cmd
    guard, hook = _both(fws, run["b"], cmd, run["clone"])
    assert not guard.allow and _behavior(hook) == "deny", cmd


@pytest.mark.parametrize("cmd", [COMMIT, "git checkout fx/{c}", "git switch -q fx/{c}", "git add a.txt",
                                 "git restore a.txt", "git --no-pager log -p", "git status", "git branch --show-current",
                                 "git diff HEAD", "git rev-parse HEAD", "git ls-files", "git blame a.txt"])
def test_the_allowlist_passes_the_sessions_own_work(fws, run, cmd):
    cmd = cmd.replace("{c}", run["cid"].lower())
    assert permits.commit_refusal(fws, run["b"], run["clone"], cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    "git push origin fx/{c}", "git push origin HEAD:refs/heads/fx/{c}", "git push /tmp/x fx/{c}",
    "git push https://example.invalid/x.git fx/{c}", "git fetch origin", "git fetch --update-head-ok origin main",
    "git remote add x /tmp/x", "git remote set-url origin /tmp/x", "git config user.name x", "git tag -l",
    "git reset --hard main", "git submodule update", "git gc --prune=now", "git reflog expire --all",
    "git am p.patch", "git apply --directory=x p.patch", "git checkout main", "git switch other",
    "git checkout -b fx/{c}-2", "git --git-d=/tmp/x status", "git --work-t=/tmp status", "git -C /tmp status",
    "git --exec-path=/tmp status", "git --namespace=x status", "git -c core.pager=x log", "git ci -m x",
    "env git push origin fx/{c}", "command git push origin fx/{c}", "sh -c 'git push origin fx/{c}'",
    "bash -lc \"git remote add x /tmp\"", "GIT_DIR=x git status", "GIT_TRACE=1 git status",
])
def test_everything_else_is_refused_by_default(fws, run, cmd):
    cmd = cmd.replace("{c}", run["cid"].lower())
    assert permits._git_commit(cmd), cmd
    assert permits.commit_refusal(fws, run["b"], run["clone"], cmd), cmd
    guard, hook = _both(fws, run["b"], cmd, run["clone"])
    assert not guard.allow and _behavior(hook) == "deny", cmd


def test_reads_pass_for_a_session_in_the_shared_checkout_and_writes_do_not(fws, run):
    b = {**run["b"], "start": str(fws.root.resolve())}  # a planner or a no-commit child
    assert permits.commit_refusal(fws, b, fws.root, "git status") is None
    assert permits.commit_refusal(fws, b, fws.root, "git log --oneline") is None
    assert permits.commit_refusal(fws, b, fws.root, COMMIT)


# -- 8: commit messages are checked by the release --------------------------------------------------------------

def test_a_child_commit_with_a_bad_message_fails_the_merge(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    (clone / "x.txt").write_text("x\n", encoding="utf-8")
    _g(clone, "add", "x.txt")
    _g(clone, "commit", "-q", "-m", "wip", "-m", "Co-Authored-By: Claude <noreply@anthropic.com>")
    _to_testing(fa, cid, close_tasks)
    fake = RecipeFake()
    lines = fr.tick(fws, human, fake)
    assert any("commit-msg check refuses" in x for x in lines), lines
    from orch.core import factory_report
    assert [r["code"] for r in factory_report.stopped(fws, store.load(fws, eid)[1])] == ["release-failed"]
    assert not fake.calls


# -- 9 and 10: git dirs orch's clones do not take ------------------------------------------------------------------

@pytest.mark.parametrize("plant,why", [
    ("shallow", "shallow"), ("commondir", "commondir"), ("gitdir", "names another git dir"),
    ("info/grafts", "grafts"), ("objects-link", "objects is missing, a link"), ("refs-link", "refs is missing"),
])
def test_odd_git_dirs_are_refused_in_the_source_and_in_the_clone(fws, fa, fh, human, run, tmp_path, plant, why):
    def do(git):
        if plant.endswith("-link"):
            name = plant[:-5]
            shutil.move(str(git / name), str(tmp_path / f"{name}-{git.parent.name}"))
            (git / name).symlink_to(tmp_path / f"{name}-{git.parent.name}", target_is_directory=True)
        else:
            (git / plant).parent.mkdir(parents=True, exist_ok=True)
            (git / plant).write_text("x\n", encoding="utf-8")
    do(run["clone"] / ".git")
    assert why in fc.own_clone(fws, run["clone"], run["cid"]) and fc.verify(fws, run["cid"])[0] is None
    do(fws.root / ".git")
    other = _child(fa, run["eid"])
    path, msg = fc.ensure(fws, human, other)
    assert path is None and why in msg and fc.clonable(fws)[0] is False


# -- 12 and 13: removal by tombstone, and the clone lock ---------------------------------------------------------

def test_a_partial_removal_keeps_the_record_and_says_what_is_left(fws, run, human, monkeypatch):
    fs.end(fws, run["b"]["session"])

    def fail(dfd, name, dev):
        raise OSError("operation not permitted (an immutable file)")
    monkeypatch.setattr(fc, "_rmtree_fd", fail)
    with pytest.raises(ValidationError, match="only partly removed"):
        fc.clean(fws, human, run["cid"])
    assert fc.record(fws, run["cid"]) is not None
    left = [n for n in os.listdir(run["clone"].parent) if n.startswith(".removing-")]
    assert left and not run["clone"].exists()
    with pytest.raises(ValidationError, match="partly removed clone"):
        fc.clean(fws, human, run["cid"])  # never "removed by hand" while content remains
    assert fc.record(fws, run["cid"]) is not None


def test_a_mount_point_inside_stops_the_removal(fws, run):
    pfd = fc._walk(fc._parent_parts(fws, run["cid"]))
    try:
        with pytest.raises(OSError, match="mount point"):
            fc._rmtree_fd(pfd, "repo", -1)  # a device that is not the clone's: nothing below is removed
    finally:
        os.close(pfd)
    assert (run["clone"] / ".git").is_dir()


def test_a_clone_removed_by_hand_drops_only_the_record(fws, run, human):
    fs.end(fws, run["b"]["session"])
    shutil.rmtree(run["clone"])
    assert fc.clean(fws, human, run["cid"]) and fc.record(fws, run["cid"]) is None


def test_clean_waits_for_the_runners_clone_lock(fws, run, human, monkeypatch):
    fs.end(fws, run["b"]["session"])
    monkeypatch.setattr(fc, "LOCK_TIMEOUT", 0.5)
    held, release = threading.Event(), threading.Event()

    def runner():
        with fc._clone_lock(fws, run["cid"]):
            held.set()
            release.wait(5)
    t = threading.Thread(target=runner)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(UsageError, match="working on the clone"):
            fc.clean(fws, human, run["cid"])
        assert run["clone"].is_dir() and fc.record(fws, run["cid"]) is not None
    finally:
        release.set()
        t.join()


# -- 16 and 17: the round goes on; the case probe stays out of the work tree -----------------------------------

def test_one_clone_per_round_within_a_time_budget(fws, fa, fh, human, monkeypatch):
    eid, d = _epic(fa, fh, human, fws)
    a, b = _child(fa, eid), _child(fa, eid)
    budgets = []
    real = fr.run_command

    def timed(argv, cwd, env, timeout, **kw):
        if "clone" in argv or "checkout" in argv:
            budgets.append(timeout)
        return real(argv, cwd, env, timeout, **kw)
    monkeypatch.setattr(fr, "run_command", timed)
    fake = Fake()
    lines = _tick(fws, human, fake)
    assert len(fake.started) == 1 and any("one clone per round" in x for x in lines), lines
    assert budgets and max(budgets) <= factory_runner.ROUND_CLONE_SECONDS
    _tick(fws, human, fake)
    assert len(fake.started) == 2


def test_the_case_probe_never_reads_the_work_tree(fws, run, human, monkeypatch):
    seen = []
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: (seen.append(str(p)), real(p))[1])
    assert fc.ensure(fws, human, run["cid"])[0] == run["clone"]
    assert seen and not any(str(fc.root()) in s or str(fc.root().resolve()) in s for s in seen)


# -- one reading per question: the guard and the hook decide the same, whatever the spelling ---------------------

@pytest.mark.parametrize("cmd", [
    'sh -c "git push origin main"', "bash -c 'git branch -f main HEAD'", "git -c alias.p=push p origin main",
    "$(which git) push origin main", "git pu\\\nsh origin main", "git push", "git push origin",
    "git checkout -B main", "git switch -C main", "git checkout --orphan=x", "git worktree add -b x ../x",
    "git fast-import", "git send-pack /tmp/x main", "Git push origin main", "/usr/bin/git push origin main",
    "true;git push origin main", "echo hi&&git branch -D other", "GIT_DIR=x  git status", "git --namespace=x push",
])
def test_every_spelling_is_gated_and_both_gates_agree(fws, run, cmd):
    assert permits._git_commit(cmd), cmd
    guard, hook = _both(fws, run["b"], cmd, run["clone"])
    assert not guard.allow and _behavior(hook) == "deny", cmd
    assert permits.commit_refusal(fws, run["b"], run["clone"], cmd), cmd


def test_a_message_cannot_hide_behind_a_separator(fws, fa, fh, human, close_tasks, remote):  # noqa: F811
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    (clone / "x.txt").write_text("x\n", encoding="utf-8")
    _g(clone, "add", "x.txt")
    good = f"{cid} work\n\nWhat: a\nWhy: b\nRisk: c"
    # the old check split `git log` output on 0x1e/0x1f: this attribution line sat in a forged "commit id" field
    msg = f"{good}\n\x1e\nCo-Authored-By: Claude <noreply@anthropic.com>\n\x1f\n{good}"
    _g(clone, "commit", "-q", "--cleanup=verbatim", "-m", msg)
    _to_testing(fa, cid, close_tasks)
    lines = fr.tick(fws, human, RecipeFake())
    assert any("commit-msg check refuses" in x and "attribution" in x for x in lines), lines
