"""H11d: the runner's worktree git is confined under ``<repo>.worktrees`` and never follows a symlink."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orch.runner import wtgit
from orch.runner.supervise import SpawnError

WS8 = "0123abcd"
NAME = f"{WS8}-ORCH-1"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(cwd: Path, *a: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), **GIT_ENV, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path):
    root = Path(os.path.realpath(tmp_path)) / "w"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "c")
    st = repo.stat()
    pin = {"path": str(repo), "dev": st.st_dev, "ino": st.st_ino}
    own = {"HOME": str(root), "PATH": os.environ["PATH"]}

    def args(op: str, name: str = NAME, **kw):
        a = {"op": op, "repo_pin": pin, "path": f"{repo}.worktrees/{name}", "branch": "feat-x", **kw}
        if op == "worktree.add":
            a["base"] = _git(repo, "rev-parse", "HEAD")
        return a

    return root, repo, own, args


def _code(exc: pytest.ExceptionInfo[SpawnError]) -> str:
    return exc.value.code


@pytest.mark.parametrize(
    "name", ["../evil", "..", "/abs/x", f"{NAME}/../x", f"{NAME}\0", "a" * 8 + "-x", NAME + "/sub", ""]
)
def test_bad_names_are_refused_before_anything_is_created(world, name):
    root, repo, own, args = world
    a = args("worktree.add", "x")
    a["path"] = f"{repo}.worktrees/{name}"
    with pytest.raises(SpawnError):
        wtgit.run_git(a, own)
    assert not Path(f"{repo}.worktrees").exists()


def test_a_path_outside_the_repos_worktrees_dir_is_refused(world):
    root, repo, own, args = world
    for path in (f"{root}/other.worktrees/{NAME}", f"{root}/{NAME}", f"{repo}/{NAME}", f"{repo}.worktrees/../{NAME}"):
        a = args("worktree.add")
        a["path"] = path
        with pytest.raises(SpawnError):
            wtgit.run_git(a, own)
    assert not (root / "other.worktrees").exists()


def test_add_and_remove_work_normally(world):
    root, repo, own, args = world
    assert wtgit.run_git(args("worktree.add"), own) == {"ok": True}
    assert (Path(f"{repo}.worktrees") / NAME / "f").exists()
    assert wtgit.run_git(args("worktree.remove"), own) == {"ok": True}
    assert not (Path(f"{repo}.worktrees") / NAME).exists()


def test_a_symlinked_worktrees_dir_is_refused_and_nothing_lands_behind_it(world):
    root, repo, own, args = world
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, f"{repo}.worktrees")
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(args("worktree.add"), own)
    assert _code(e) == "launch.cwd_changed"
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_intermediate_directory_is_refused(world):
    root, repo, own, args = world
    real = root / "real"
    real.mkdir()
    (real / "repo").mkdir()
    _git(real / "repo", "init", "-q")
    link = root / "link"
    os.symlink(real, link)
    st = (real / "repo").stat()
    pin = {"path": str(link / "repo"), "dev": st.st_dev, "ino": st.st_ino}
    a = {
        "op": "worktree.add",
        "repo_pin": pin,
        "path": f"{link}/repo.worktrees/{NAME}",
        "branch": "b",
        "base": "0" * 40,
    }
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(a, own)
    assert _code(e) == "launch.cwd_changed"
    assert not (real / "repo.worktrees").exists()


def test_a_symlinked_target_is_refused_on_add_and_remove_and_never_followed(world):
    root, repo, own, args = world
    victim = root / "victim"
    victim.mkdir()
    (victim / "precious").write_text("keep")
    wt = Path(f"{repo}.worktrees")
    wt.mkdir()
    os.symlink(victim, wt / NAME)
    for op in ("worktree.add", "worktree.remove"):
        with pytest.raises(SpawnError) as e:
            wtgit.run_git(args(op, force=True), own)
        assert _code(e) == "launch.cwd_changed"
    assert (victim / "precious").read_text() == "keep"
    assert (wt / NAME).is_symlink()


def test_removal_of_a_gone_directory_only_prunes(world):
    root, repo, own, args = world
    wtgit.run_git(args("worktree.add"), own)
    import shutil

    shutil.rmtree(Path(f"{repo}.worktrees") / NAME)
    assert wtgit.run_git(args("worktree.remove"), own) == {"ok": True}
    assert NAME not in _git(repo, "worktree", "list")


def test_a_symlink_swapped_in_between_check_and_use_is_detected(world, monkeypatch):
    """The worktrees dir is replaced by a symlink after the fds were verified and before git ran: the pre-spawn check
    refuses it (and nothing is written behind the link)."""
    root, repo, own, args = world
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    real_check = wtgit._Confined.check_cwd
    swapped = []

    def swap_then_check(self):
        if not swapped:
            swapped.append(1)
            d = Path(self.dir_path)
            d.rename(root / "moved")
            os.symlink(elsewhere, d)
        real_check(self)

    monkeypatch.setattr(wtgit._Confined, "check_cwd", swap_then_check)
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(args("worktree.add"), own)
    assert _code(e) == "launch.cwd_changed"
    assert list(elsewhere.iterdir()) == []


def test_a_swap_during_git_is_detected_afterwards(world, monkeypatch):
    root, repo, own, args = world
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    real_spawn = wtgit._spawn

    def spawn_then_swap(*a, **kw):
        out = real_spawn(*a, **kw)
        d = Path(f"{repo}.worktrees")
        d.rename(root / "moved")
        os.symlink(elsewhere, d)
        return out

    monkeypatch.setattr(wtgit, "_spawn", spawn_then_swap)
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(args("worktree.add"), own)
    assert _code(e) == "launch.cwd_changed"


@pytest.mark.parametrize("which", ["parent", "worktrees"])
@pytest.mark.parametrize("mode", [0o777, 0o775, 0o757])
def test_a_group_or_other_writable_directory_is_refused(world, which, mode):
    """Opus #439 F2: only the person's own account may rename entries in the repo's parent or <repo>.worktrees."""
    root, repo, own, args = world
    target = root if which == "parent" else Path(f"{repo}.worktrees")
    target.mkdir(exist_ok=True)
    os.chmod(target, mode)
    try:
        with pytest.raises(SpawnError) as e:
            wtgit.run_git(args("worktree.add"), own)
        assert _code(e) == "launch.cwd_changed"
        assert not (Path(f"{repo}.worktrees") / NAME).exists()
    finally:
        os.chmod(target, 0o755)


def test_a_directory_of_another_owner_is_refused(world, monkeypatch):
    root, repo, own, args = world
    monkeypatch.setattr(wtgit.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(args("worktree.add"), own)
    assert _code(e) == "launch.cwd_changed"
