"""Opus review G5 (#1175): adversarial probes of ``gitsafe.market_local_export`` / ``orch addon pack`` / ``new``.

Tests named ``test_gap_*`` assert the SAFE behaviour and are expected to FAIL on 9ca61679 (they document a finding)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orch.market import author
from tests.market.test_author import commit_files
from tests.market.test_export import git

NAME = "changelog"


def _repo(p: Path) -> Path:
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    return p


# ---------------------------------------------------------------- no program runs


def test_hooks_from_an_allowed_hookspath_never_run(tmp_path):
    repo = _repo(tmp_path / "repo")
    commit_files(repo, author.scaffold(NAME))
    hooks = repo / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    for h in ("pre-commit", "post-checkout", "post-index-change", "reference-transaction", "post-rewrite",
              "pre-auto-gc", "fsmonitor-watchman", "post-merge"):  # fmt: skip
        f = hooks / h
        f.write_text(f"#!/bin/sh\necho {h} >> {marker}\n")
        f.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))  # on the allow-list (husky); -c overrides it
    author.pack(str(repo))
    assert not marker.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("hook.x.command", "touch {m}"),
        ("hook.x.event", "pre-commit"),
        ("include.path", "{d}/evil.cfg"),
        ("includeIf.gitdir:/.path", "{d}/evil.cfg"),
        ("filter.x.clean", "touch {m}"),
        ("filter.x.smudge", "touch {m}"),
        ("filter.x.process", "touch {m}"),
        ("core.fsmonitor", "touch {m}"),
        ("core.sshCommand", "touch {m}"),
        ("core.worktree", "/"),
        ("core.alternateRefsCommand", "touch {m}"),
        ("uploadpack.packObjectsHook", "touch {m}"),
        ("remote.origin.uploadpack", "touch {m}"),
        ("credential.helper", "!touch {m}"),
    ],
)
def test_a_program_key_in_the_repo_config_is_refused_and_never_runs(tmp_path, key, value):
    repo = _repo(tmp_path / "repo")
    files = author.scaffold(NAME)
    files[".gitattributes"] = b"* filter=x\n"
    commit_files(repo, files)
    marker = tmp_path / "ran"
    (tmp_path / "evil.cfg").write_text(f'[filter "x"]\n\tclean = touch {marker}\n\tsmudge = touch {marker}\n')
    git(repo, "config", key, value.format(m=marker, d=tmp_path))
    with pytest.raises(author.AuthorError) as e:
        author.pack(str(repo))
    assert e.value.code == "invalid.input" and not marker.exists()


def test_a_worktree_config_key_is_gated_too(tmp_path):
    repo = _repo(tmp_path / "repo")
    commit_files(repo, author.scaffold(NAME))
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "core.fsmonitor", f"touch {tmp_path / 'ran'}")
    with pytest.raises(author.AuthorError):
        author.pack(str(repo))
    assert not (tmp_path / "ran").exists()


def test_the_persons_global_filter_never_runs(tmp_path, monkeypatch):
    """A global ``filter.lfs``-like driver (the person's ~/.gitconfig) is not read by the closed environment."""
    repo = _repo(tmp_path / "repo")
    files = {f"pkg/{k}": v for k, v in author.scaffold(NAME).items()}
    files[".gitattributes"] = b"*.py filter=g\n"
    commit_files(repo, files)
    marker = tmp_path / "ran"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[filter "g"]\n\tsmudge = touch {marker}\n\tclean = touch {marker}\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    author.pack(str(repo), subdir="pkg")
    assert not marker.exists()


# ---------------------------------------------------------------- reads outside the author's repository


def _other_repo_with_secret(tmp_path: Path) -> tuple[Path, str]:
    other = _repo(tmp_path / "other")
    files = author.scaffold(NAME)
    files["secret.txt"] = b"from another repository\n"
    return other, commit_files(other, files, "other")


def test_gap_alternates_make_the_export_read_another_repositorys_objects(tmp_path):
    """objects/info/alternates is not config, so the gate never sees it: a repository can borrow every object of any
    readable repository, and ``pack``/``addon dev`` export a commit that lives only there."""
    other, commit = _other_repo_with_secret(tmp_path)
    repo = _repo(tmp_path / "repo")
    (repo / ".git" / "objects" / "info" / "alternates").write_text(f"{other / '.git' / 'objects'}\n")
    git(repo, "update-ref", "refs/heads/main", commit)
    with pytest.raises(author.AuthorError):
        author.pack(str(repo))


def test_gap_a_symlinked_dot_git_reads_a_git_dir_outside_the_folder(tmp_path):
    other, _ = _other_repo_with_secret(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    os.symlink(other / ".git", folder / ".git")
    with pytest.raises(author.AuthorError):
        author.pack(str(folder))


def test_a_gitfile_pointing_elsewhere_is_refused(tmp_path):
    """M1: a ``.git`` file (``gitdir: …``) points outside the folder, so it is refused (use the main repo)."""
    other, commit = _other_repo_with_secret(tmp_path)
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / ".git").write_text(f"gitdir: {other / '.git'}\n")
    with pytest.raises(author.AuthorError):
        author.pack(str(folder))


# ---------------------------------------------------------------- orch addon new


def test_new_never_follows_a_planted_symlink(tmp_path, monkeypatch):
    from orch.cli import addon_market as am

    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    os.symlink(outside, work / NAME)
    monkeypatch.chdir(work)
    import io

    out = io.StringIO()
    rc = am.main(["new", NAME, "--json"], env={}, out=out)
    assert rc == 2 and list(outside.iterdir()) == []


def test_new_is_offline_and_touches_no_store(tmp_path, monkeypatch):
    from orch.cli import addon_market as am

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(am, "_connect", lambda *a: (_ for _ in ()).throw(AssertionError("host asked")))
    import io

    assert am.main(["new", NAME], env={"ORCH_SESSION": "s", "ORCH_GRANT": "g"}, out=io.StringIO()) == 0
    assert sorted(p.name for p in (tmp_path / NAME).iterdir()) == ["README.md", "main.py", "orch-addon.json"]


def test_pack_rev_cannot_become_an_option_or_a_reflog(tmp_path):
    repo = _repo(tmp_path / "repo")
    commit_files(repo, author.scaffold(NAME))
    for rev in ("--output=/tmp/x", "HEAD@{0}", "main:orch-addon.json", "HEAD~1", "a" * 39, "HEAD^{tree}"):
        with pytest.raises(author.AuthorError):
            author.pack(str(repo), rev)
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True).stdout == b""
