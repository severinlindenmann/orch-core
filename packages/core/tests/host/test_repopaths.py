"""The Repos add-on's host checks: what a declared path must be, and the read-only look at one (orch.repopaths)."""

from __future__ import annotations

import os

import pytest

from orch import repopaths
from orch.layout import Layout


def mk(tmp_path, name="proj", head="ref: refs/heads/main\n"):
    d = tmp_path / name
    (d / ".git").mkdir(parents=True)
    if head is not None:
        (d / ".git" / "HEAD").write_text(head)
    return d


@pytest.fixture
def layout(tmp_path):
    (tmp_path / "ws" / ".orch").mkdir(parents=True)
    return Layout.new_at(tmp_path / "ws")


def refused(layout, path):
    with pytest.raises(repopaths.RepoPathError) as e:
        repopaths.check_declared(layout, path)
    return str(e.value)


def test_a_git_folder_is_accepted(layout, tmp_path):
    repopaths.check_declared(layout, str(mk(tmp_path)))
    d = tmp_path / "linked"
    d.mkdir()
    (d / ".git").write_text("gitdir: /elsewhere\n")  # a linked working copy has a .git FILE
    repopaths.check_declared(layout, str(d))


@pytest.mark.parametrize("bad", ["", "proj", "./proj", "/a/../b", "/a//b", "/a/./b", "/a\nb", None, 3, "x" * 2000])
def test_a_path_must_be_absolute_and_plain(layout, bad):
    refused(layout, bad)


def test_it_must_exist_be_a_folder_and_a_git_repo(layout, tmp_path):
    assert "does not exist" in refused(layout, str(tmp_path / "nope"))
    f = tmp_path / "file"
    f.write_text("x")
    assert "not a folder" in refused(layout, str(f))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a git repository" in refused(layout, str(plain))
    link = tmp_path / "link"
    link.mkdir()
    os.symlink(str(plain), str(link / ".git"))  # a .git that is a link is not a repository marker
    assert "not a git repository" in refused(layout, str(link))


def test_not_the_home_folder_nor_the_root(layout, tmp_path, monkeypatch):
    home = mk(tmp_path, "home")
    monkeypatch.setenv("HOME", str(home))  # a home folder that is itself a git repo is still refused
    assert "home folder" in refused(layout, str(home))
    os.symlink(str(home), str(tmp_path / "homelink"))
    assert "home folder" in refused(layout, str(tmp_path / "homelink"))  # nor through a link
    assert "home folder" in refused(layout, "/")


def test_not_inside_the_orch_store(layout, tmp_path):
    inner = layout.store / "records" / "r"
    (inner / ".git").mkdir(parents=True)
    assert "orch's own data" in refused(layout, str(layout.store))
    assert "orch's own data" in refused(layout, str(inner))
    os.symlink(str(inner), str(tmp_path / "sneaky"))
    assert "orch's own data" in refused(layout, str(tmp_path / "sneaky"))


def test_observe_reads_the_branch_without_git(layout, tmp_path):
    d = mk(tmp_path, "ws/r1")  # a folder inside the project, declared relative
    assert repopaths.observe(layout, "r1") == {"state": "present", "branch": "main"}
    assert repopaths.observe(layout, str(d)) == {"state": "present", "branch": "main"}
    (d / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert repopaths.observe(layout, "r1") == {"state": "present", "branch": None}  # detached
    (d / ".git" / "HEAD").write_text("ref: refs/heads/--upload-pack=x y\n")
    assert repopaths.observe(layout, "r1")["branch"] is None  # not a branch name
    assert repopaths.observe(layout, "nope") == {"state": "missing", "branch": None}
    (tmp_path / "ws" / "plain").mkdir()
    assert repopaths.observe(layout, "plain") == {"state": "not_git", "branch": None}
    (tmp_path / "ws" / "file").write_text("x")
    assert repopaths.observe(layout, "file") == {"state": "missing", "branch": None}


def test_observe_never_follows_a_link_for_head(layout, tmp_path):
    d = mk(tmp_path, "ws/r2", head=None)
    secret = tmp_path / "secret"
    secret.write_text("ref: refs/heads/leaked\n")
    os.symlink(str(secret), str(d / ".git" / "HEAD"))
    assert repopaths.observe(layout, "r2") == {"state": "not_git", "branch": None}
    fifo = tmp_path / "ws" / "r3"
    (fifo / ".git").mkdir(parents=True)
    os.mkfifo(str(fifo / ".git" / "HEAD"))
    assert repopaths.observe(layout, "r3") == {"state": "not_git", "branch": None}  # a FIFO never blocks the host
    (tmp_path / "ws" / "r4").mkdir()
    os.symlink(str(d), str(tmp_path / "ws" / "r4" / ".git"))
    assert repopaths.observe(layout, "r4") == {"state": "not_git", "branch": None}  # a linked .git is not followed


def test_a_linked_git_file_is_followed_and_held_to_the_same_rules(layout, tmp_path):
    inner = layout.store / "records" / "gitdata"
    inner.mkdir(parents=True)
    d = tmp_path / "linked2"
    d.mkdir()
    (d / ".git").write_text(f"gitdir: {inner}\n")
    assert "orch's own data" in refused(layout, str(d))
    bad = tmp_path / "linked3"
    bad.mkdir()
    (bad / ".git").write_text("not a gitdir line\n")
    assert "not a git repository" in refused(layout, str(bad))


def test_not_under_the_orch_config_or_state_dir(layout, tmp_path, monkeypatch):
    state = tmp_path / "orchstate"
    (state / "r" / ".git").mkdir(parents=True)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    assert "orch's own data" in refused(layout, str(state / "r"))
    cfg = tmp_path / "xdg"
    (cfg / "orch" / "r" / ".git").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    assert "orch's own data" in refused(layout, str(cfg / "orch" / "r"))


def _linked(tmp_path, name, gitdir_line):
    d = tmp_path / name
    d.mkdir()
    (d / ".git").write_text(gitdir_line)
    return str(d)


def test_a_real_git_worktree_is_accepted(layout, tmp_path):
    main = mk(tmp_path, "main")
    wt_git = main / ".git" / "worktrees" / "x"
    wt_git.mkdir(parents=True)
    repopaths.check_declared(layout, _linked(tmp_path, "wt", f"gitdir: {wt_git}\n"))


def test_a_relative_gitdir_is_resolved_against_the_folder(layout, tmp_path):
    main = mk(tmp_path, "main2")
    (main / ".git" / "worktrees" / "y").mkdir(parents=True)
    repopaths.check_declared(layout, _linked(tmp_path, "wt2", "gitdir: ../main2/.git/worktrees/y\n"))
    inner = layout.store / "records" / "gd"
    inner.mkdir(parents=True)
    rel = os.path.relpath(inner, tmp_path / "wt3")
    assert "orch's own data" in refused(layout, _linked(tmp_path, "wt3", f"gitdir: {rel}\n"))


def test_a_long_gitdir_line_is_refused_not_checked_cut_short(layout, tmp_path):
    inner = layout.store / "records" / "gd"
    inner.mkdir(parents=True)
    line = f"gitdir: {layout.project}/" + "./" * 400 + f"{layout.store.name}/records/gd\n"
    assert len(line) > 600
    assert "not a git repository" in refused(layout, _linked(tmp_path, "wt4", line))


def test_a_gitdir_through_a_link_and_dotdot_is_resolved_before_the_checks(layout, tmp_path):
    inner = layout.store / "records" / "gd"
    inner.mkdir(parents=True)
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    os.symlink(str(deep), str(tmp_path / "l"))
    # lexically l/../.. is tmp_path/.. ; through the link it is tmp_path/a/b, so the target differs from the lexical one
    target = os.path.relpath(inner, tmp_path / "a" / "b")
    line = f"gitdir: {tmp_path}/l/../../{target}\n"
    assert "orch's own data" in refused(layout, _linked(tmp_path, "wt5", line))


def test_a_relative_state_dir_is_made_absolute_like_the_product_does(layout, tmp_path, monkeypatch):
    (tmp_path / "rel" / "r" / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCH_STATE_DIR", "rel")
    assert "orch's own data" in refused(layout, str(tmp_path / "rel" / "r"))
