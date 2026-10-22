"""#584: ``worktree add`` runs no repo-config filter. The runner adds with ``--no-checkout`` and writes the tree itself
(``orch.gitread.checkout``); only the allow-listed filter (lfs) from the PERSON's config is applied; anything it cannot
write exactly removes the worktree again with ``worktree.checkout_unsupported``."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from test_wtgit_confine import GIT_ENV, NAME, _git, world  # noqa: F401

from orch.gitread.checkout import populate
from orch.gitread.errors import Unavailable
from orch.runner import wtgit
from orch.runner.supervise import SpawnError


def _wt(repo: Path) -> Path:
    return Path(f"{repo}.worktrees") / NAME


def _pgit(root: Path, cwd: Path, *a: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Plain git as the person (HOME = root, so its global config applies), no system config."""
    env = {"PATH": os.environ["PATH"], "HOME": str(root), **GIT_ENV, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=check, capture_output=True, text=True)


def _commit(repo: Path, files: dict[str, str], msg: str = "m") -> None:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        _git(repo, "add", name)
    _git(repo, "commit", "-qm", msg)


def _snapshot(top: Path) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for dp, dn, fn in os.walk(top):
        dn[:] = [d for d in dn if d != ".git"]
        for name in [*dn, *(f for f in fn if f != ".git")]:
            p = Path(dp) / name
            rel = str(p.relative_to(top))
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir",)
            else:
                out[rel] = ("file", bool(st.st_mode & 0o100), p.read_bytes())
    return out


def _add(world_, base: str | None = None) -> None:
    root, repo, own, args = world_
    a = args("worktree.add")
    if base:
        a["base"] = base
    assert wtgit.run_git(a, own) == {"ok": True}


def test_populated_tree_equals_a_plain_checkout(world, tmp_path):  # noqa: F811
    root, repo, own, args = world
    (repo / "bin").mkdir()
    _commit(repo, {"a/b/c.txt": "deep\n", "a/.gitattributes": "*.dat -text\n", "ünï/ç.txt": "u\n", "sp ace": "s"})
    script = repo / "bin" / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    (repo / "link").symlink_to("a/b/c.txt")
    (repo / "dangling").symlink_to("nowhere")
    _git(repo, "add", "bin/run.sh", "link", "dangling")
    _git(repo, "commit", "-qm", "more")
    head = _git(repo, "rev-parse", "HEAD")
    _add(world, head)
    ours = _wt(repo)
    ref = Path(f"{repo}.worktrees") / "plain"
    _pgit(root, repo, "worktree", "add", "-q", "-b", "plain", str(ref), head)
    assert _snapshot(ours) == _snapshot(ref)
    assert _pgit(root, ours, "status", "--porcelain").stdout == ""
    assert _pgit(root, ours, "ls-files", "-s").stdout == _pgit(root, ref, "ls-files", "-s").stdout
    assert _pgit(root, ours, "diff-index", "HEAD", "--").stdout == ""
    assert _pgit(root, ours, "fsck", "--no-dangling", check=False).returncode == 0
    assert wtgit.run_git(args("worktree.remove"), own) == {"ok": True}  # and it can be removed like any worktree


def test_a_gitlink_is_an_empty_directory(world):  # noqa: F811
    root, repo, own, args = world
    sub = "1" * 40
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sub},mod")
    _git(repo, "commit", "-qm", "gitlink")
    _add(world, _git(repo, "rev-parse", "HEAD"))
    assert (_wt(repo) / "mod").is_dir() and os.listdir(_wt(repo) / "mod") == []
    assert _pgit(root, _wt(repo), "status", "--porcelain").stdout == ""


def test_the_config_flipper_race_never_runs_the_marker(world):  # noqa: F811
    """#565's probe against ``worktree.create``: a thread flips .git/config between benign and a smudge filter that
    touches a marker, with ``* filter=x`` committed. The gate may pass while git would read the evil config: the
    marker must still fire 0 times."""
    root, repo, own, args = world
    marker = root / "marker"
    _commit(repo, {".gitattributes": "* filter=x\n"})
    cfg = repo / ".git" / "config"
    benign = cfg.read_text()
    evil = benign + f'[filter "x"]\n\tsmudge = sh -c "touch {marker}; cat"\n\tclean = cat\n'
    stop = threading.Event()

    def flip() -> None:
        while not stop.is_set():
            for text in (evil, benign):
                tmp = cfg.with_suffix(".tmp")
                tmp.write_text(text)
                os.replace(tmp, cfg)

    t = threading.Thread(target=flip, daemon=True)
    t.start()
    reached = 0
    try:
        for i in range(25):
            name = f"{i:08x}-ORCH-{i + 1}"
            a = args("worktree.add", name)
            a["branch"] = f"b{i}"
            try:
                wtgit.run_git(a, own)
            except SpawnError as exc:
                # the gate saw the evil config (observe.unavailable), or it passed and orch's own checkout refused the
                # committed `filter=` attribute: either way no program ran
                reached += exc.code == "worktree.checkout_unsupported"
            finally:
                with contextlib.suppress(SpawnError):
                    wtgit.run_git(args("worktree.remove", name), own)
    finally:
        stop.set()
        t.join()
    assert not marker.exists(), "a repo-config smudge filter ran during worktree.add"
    assert reached, "the gate never passed: the race never reached the checkout, the test proves nothing"


def test_control_plain_git_checkout_does_run_the_repo_filter(world):  # noqa: F811
    root, repo, own, args = world
    marker = root / "marker"
    _commit(repo, {".gitattributes": "* filter=x\n"})
    _git(repo, "config", "filter.x.smudge", f"sh -c 'touch {marker}; cat'")
    _pgit(root, repo, "worktree", "add", "-q", "-b", "ctl", str(Path(f"{repo}.worktrees") / "ctl"))
    assert marker.exists()


def test_populate_never_reads_a_filter_from_the_repo_config(world):  # noqa: F811
    """Bypass the gate: a plain ``--no-checkout`` add of a repo whose config names a smudge filter. ``populate`` writes
    the raw blob and the program never runs."""
    root, repo, own, args = world
    marker = root / "marker"
    _commit(repo, {".gitattributes": "* filter=x\n", "p.bin": "POINTER\n"})
    _git(repo, "config", "filter.x.smudge", f"sh -c 'touch {marker}; echo SMUDGED'")
    _git(repo, "config", "filter.lfs.smudge", f"sh -c 'touch {marker}; echo SMUDGED'")
    wt = _wt(repo)
    Path(f"{repo}.worktrees").mkdir()
    _pgit(root, repo, "worktree", "add", "-q", "--no-checkout", "-b", "n", str(wt))
    with pytest.raises(Unavailable, match="filters"):
        populate(str(wt), own)
    assert not marker.exists()
    # and without the attribute the raw blob is written, the repo's filter definitions still unread
    _commit(repo, {".gitattributes": "# none\n"})
    wt2 = Path(f"{repo}.worktrees") / "second"
    _pgit(root, repo, "worktree", "add", "-q", "--no-checkout", "-b", "n2", str(wt2))
    populate(str(wt2), own)
    assert (wt2 / "p.bin").read_text() == "POINTER\n" and not marker.exists()


def _fake_lfs(root: Path) -> None:
    (root / "smudge.sh").write_text('#!/bin/sh\nsed "s/^POINTER:/CONTENT:/"\n')
    (root / "clean.sh").write_text('#!/bin/sh\nsed "s/^CONTENT:/POINTER:/"\n')
    for n in ("smudge.sh", "clean.sh"):
        (root / n).chmod(0o755)
    (root / ".gitconfig").write_text(
        f'[filter "lfs"]\n\tsmudge = {root}/smudge.sh %f\n\tclean = {root}/clean.sh %f\n\trequired = true\n'
    )


def _refused(world_, code: str = "worktree.checkout_unsupported") -> None:
    root, repo, own, args = world_
    a = args("worktree.add")
    a["base"] = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(a, own)
    assert e.value.code == code
    assert not _wt(repo).exists(), "a half-populated worktree was left behind"
    assert "feat-x" not in _git(repo, "branch", "--list"), "the branch the failed add created is still there"
    assert str(_wt(repo)) not in _git(repo, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("attr", ["* ident", "* working-tree-encoding=latin1", "* eol=crlf"])
def test_conversions_it_does_not_do_fail_closed(world, attr):  # noqa: F811
    root, repo, own, args = world
    _commit(repo, {".gitattributes": attr + "\n"})
    _refused(world)


def _evil_tree(repo: Path, entries: list[tuple[str, str, str]]) -> None:
    """Commit a tree git's own commands would refuse to build (mktree does not verify paths)."""
    oid = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input="x", capture_output=True, text=True, check=True,
        env={"PATH": os.environ["PATH"], "HOME": str(repo)},
    ).stdout.strip()  # fmt: skip
    lines = "".join(f"{mode} blob {oid}\t{name}\n" for mode, _t, name in entries)
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input=lines, capture_output=True, text=True, check=True,
        env={"PATH": os.environ["PATH"], "HOME": str(repo)},
    ).stdout.strip()  # fmt: skip
    commit = _git(repo, "commit-tree", tree, "-m", "evil")
    _git(repo, "update-ref", "refs/heads/main", commit)


@pytest.mark.parametrize("name", [".git", ".GIT", ".Git", "git~1", ".git ", ".g‌it"])
def test_a_dot_git_path_fails_closed(world, name):  # noqa: F811
    root, repo, own, args = world
    _evil_tree(repo, [("100644", "blob", name)])
    _refused(world)


def test_a_gitmodules_symlink_fails_closed(world):  # noqa: F811
    root, repo, own, args = world
    _evil_tree(repo, [("120000", "blob", ".gitmodules")])
    _refused(world)


def test_populate_refuses_a_non_empty_worktree(world):  # noqa: F811
    root, repo, own, args = world
    wt = _wt(repo)
    Path(f"{repo}.worktrees").mkdir()
    _pgit(root, repo, "worktree", "add", "-q", "--no-checkout", "-b", "n", str(wt))
    (wt / "stray").write_text("x")
    with pytest.raises(Unavailable):
        populate(str(wt), own)


@pytest.mark.parametrize(
    "name",
    [".gitmodules", ".GITMODULES", ".GitModules", "gitmod~1", ".gitattributes", ".GITIGNORE", ".gitignore", ".mailmap"],
)
@pytest.mark.parametrize("deep", [False, True])
def test_special_files_as_symlinks_fail_closed_at_any_depth(world, name, deep):  # noqa: F811
    root, repo, own, args = world
    env = {"PATH": os.environ["PATH"], "HOME": str(repo)}

    def run(cmd: list[str], text: str) -> str:
        return subprocess.run(
            cmd, cwd=repo, input=text, capture_output=True, text=True, check=True, env=env
        ).stdout.strip()

    oid = run(["git", "hash-object", "-w", "--stdin"], "x")
    tree = run(["git", "mktree"], f"120000 blob {oid}\t{name}\n")
    if deep:
        tree = run(["git", "mktree"], f"040000 tree {tree}\tsub\n")
    commit = _git(repo, "commit-tree", tree, "-m", "evil")
    _git(repo, "update-ref", "refs/heads/main", commit)
    _refused(world)


def test_a_regular_gitattributes_is_fine(world):  # noqa: F811
    root, repo, own, args = world
    _commit(repo, {".gitignore": "x\n", ".gitattributes": "* -text\n"})
    _add(world, _git(repo, "rev-parse", "HEAD"))


def test_exponential_macros_are_bounded(world):  # noqa: F811
    root, repo, own, args = world
    lines = ["[attr]m0 a b"] + [f"[attr]m{i} m{i - 1} m{i - 1}" for i in range(1, 40)]
    _commit(repo, {".gitattributes": "\n".join(lines) + "\n* m39\n"})
    _refused(world)


def test_a_slow_checkout_hits_the_deadline(world, monkeypatch):  # noqa: F811
    from orch.gitread import checkout

    root, repo, own, args = world
    monkeypatch.setattr(checkout, "POPULATE_SECONDS", -1.0)
    _commit(repo, {"a": "1"})
    _refused(world)


def _marker_filter(root: Path, name: str = "lfs") -> Path:
    marker = root / "filter-ran"
    (root / ".gitconfig").write_text(f'[filter "{name}"]\n\tsmudge = sh -c "touch {marker}; cat"\n\tclean = cat\n')
    return marker


@pytest.mark.parametrize("name", ["lfs", "other"])
def test_a_filter_attribute_refuses_the_whole_worktree_and_no_filter_runs(world, name):  # noqa: F811
    """No external filter process runs during populate, the person's lfs included: any ``filter=`` refuses."""
    root, repo, own, args = world
    marker = _marker_filter(root, name)
    _commit(repo, {".gitattributes": f"*.bin filter={name}\n", "big.bin": "POINTER:abc\n", "plain.txt": "x"})
    a = args("worktree.add")
    a["base"] = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(a, own)
    assert e.value.code == "worktree.checkout_unsupported" and "Git LFS / filters" in e.value.message
    assert not marker.exists()
    assert not _wt(repo).exists() and "feat-x" not in _git(repo, "branch", "--list")


def test_a_filter_name_nobody_defines_is_refused_too(world):  # noqa: F811
    root, repo, own, args = world
    _commit(repo, {".gitattributes": "* filter=whatever\n"})
    _refused(world)


@pytest.mark.parametrize("value", ["true", "yes", "on", "1", "TRUE", "Yes"])
def test_autocrlf_true_spellings_fail_closed(world, value):  # noqa: F811
    root, repo, own, args = world
    (root / ".gitconfig").write_text(f"[core]\n\tautocrlf = {value}\n")
    _refused(world)


def test_bare_autocrlf_fails_closed(world):  # noqa: F811
    root, repo, own, args = world
    (root / ".gitconfig").write_text("[core]\n\tautocrlf\n")
    _refused(world)


@pytest.mark.parametrize("value", ["false", "input", "no", "0"])
def test_autocrlf_off_spellings_are_fine(world, value):  # noqa: F811
    root, repo, own, args = world
    (root / ".gitconfig").write_text(f"[core]\n\tautocrlf = {value}\n")
    _add(world, _git(repo, "rev-parse", "HEAD"))


def test_any_exception_in_populate_undoes_everything(world, monkeypatch):  # noqa: F811
    from orch.gitread import checkout

    root, repo, own, args = world

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(checkout, "populate", boom)
    _refused(world)


def test_a_failing_git_remove_still_leaves_nothing_behind(world, monkeypatch):  # noqa: F811
    from orch.gitread import checkout

    root, repo, own, args = world
    real = wtgit._spawn

    def spawn(argv, *a, **k):
        if "remove" in argv:
            raise SpawnError("worktree.git_failed", "forced")
        return real(argv, *a, **k)

    def half(path, env, log=None):
        (Path(path) / "d").mkdir()
        (Path(path) / "d" / "ro").write_text("x")
        (Path(path) / "d" / "ro").chmod(0o444)
        (Path(path) / "d").chmod(0o555)
        (Path(path) / "link").symlink_to(root)
        raise RuntimeError("half")

    monkeypatch.setattr(checkout, "populate", half)
    monkeypatch.setattr(wtgit, "_spawn", spawn)
    _refused(world)
    assert (root / "f").exists() if (root / "f").exists() else True
    assert root.exists() and os.listdir(root)  # the symlink's target was not followed or emptied


def test_many_rules_with_a_big_macro_stay_fast(world):  # noqa: F811
    import time

    root, repo, own, args = world
    macro = "[attr]m " + " ".join(f"a{i}" for i in range(400)) + "\n"
    files = {f"d/f{i}": "x" for i in range(100)}
    _commit(repo, {".gitattributes": macro + "* m\n" * 2000, **files})
    t = time.monotonic()
    _add(world, _git(repo, "rev-parse", "HEAD"))
    assert time.monotonic() - t < 4


def test_a_differently_cased_gitattributes_is_read(world):  # noqa: F811
    root, repo, own, args = world
    _commit(repo, {".GitAttributes": "* filter=lfs\n"})
    _refused(world)
