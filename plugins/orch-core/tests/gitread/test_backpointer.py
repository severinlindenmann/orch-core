"""#486: git's back-pointers are checked again at read time, in the ``gitdir``/``worktree``-profile child.

With batched pinning (#471/#485) a child compromised through any repo of a batch answers for all of them; the host's
``_check_layout`` bounds a forgery to "repo B is ``<A>/.git/worktrees/<id>`` of another declared repo A", and only
the read-time check of git's own back-pointers (the worktree's ``gitdir`` file names ``<worktree>/.git``, its
``commondir`` names the commondir, the working copy's ``.git`` is a file naming that gitdir) can refuse it when the
forged pin is used."""

from __future__ import annotations

import os

import pytest

from orch.gitread.client import HostGitReader, PinStore, ReaderPool
from orch.gitread.errors import Unavailable
from orch.gitread.fs import RepoPins
from orch.gitread.pinning import pin_repo
from orch.gitread.repo import Repo


def committed(make_repo, name):
    r = make_repo(name)
    r.write("a.txt", f"{name}\n")
    r.commit()
    return r


class Forging(ReaderPool):
    """A pool whose ``pin_many`` answer for ``victim`` is replaced by ``forged`` (a compromised batch child)."""

    def __init__(self, victim: str, forged: RepoPins, **kw):
        super().__init__(**kw)
        self.victim, self.forged = victim, forged

    def call(self, principal, hello, req, **kw):
        got = super().call(principal, hello, req, **kw)
        if req["op"] == "pin_many":
            i = req["args"]["paths"].index(self.victim)
            got["results"][i] = {"pins": self.forged.to_json()}
        return got


@pytest.fixture
def attack(make_repo, tmp_path):
    """Declared repos A and B (both main repos); A has a linked worktree ``w`` that is **not** declared. A batch child
    forges B's pins as A's worktree ``w``: B's own working copy, w's gitdir, A's commondir."""
    a, b = committed(make_repo, "a"), committed(make_repo, "b")
    w = tmp_path.resolve() / "w"
    a.git("worktree", "add", "-q", str(w), "-b", "side")
    w_pins = pin_repo(str(w), [str(w), str(a.path)])
    b_pins = pin_repo(str(b.path), [str(b.path)])
    forged = RepoPins(b_pins.worktree, w_pins.gitdir, w_pins.commondir)
    host = HostGitReader(Forging(str(b.path), forged), PinStore(), tree_checks=True)
    res = host.sync([a.path, b.path])
    # the premise (#486): the forgery passes the host's layout and clash checks and reaches the pin store
    assert res == {str(a.path): None, str(b.path): None}
    assert host.pins.get(str(b.path)) == forged
    return a, b, w, w_pins, host


def test_a_main_repo_forged_as_another_repos_worktree_is_refused_at_read_time(attack):
    a, b, w, w_pins, host = attack
    with pytest.raises(Unavailable, match="back-pointer"):
        host.observe(b.path)
    with pytest.raises(Unavailable, match="back-pointer"):
        host.diff(b.path, branch="main", default_branch="main")
    with pytest.raises(Unavailable, match="back-pointer"):
        host.tree_check(b.path, "b", principal="p")
    assert host.observe(a.path)["head_ref"] == "refs/heads/main"  # the honest pin still reads


def test_a_planted_back_pointer_does_not_complete_the_forgery(attack):
    """Even with w's ``gitdir`` file re-pointed at B's ``.git`` (an agent that can write A's gitdir), B's ``.git``
    is a directory, not a ``.git`` file naming w's gitdir: refused in both profiles."""
    a, b, w, w_pins, host = attack
    with open(os.path.join(w_pins.gitdir.path, "gitdir"), "w") as f:
        f.write(f"{b.path}/.git\n")
    with pytest.raises(Unavailable, match=r"\.git"):
        host.observe(b.path)
    with pytest.raises(Unavailable, match=r"\.git"):
        host.tree_check(b.path, "b", principal="p")


# ---------------------------------------------------------------- legitimate linked worktrees still read


@pytest.fixture
def declared_wt(make_repo, tmp_path):
    a = committed(make_repo, "a")
    w = tmp_path.resolve() / "w"
    a.git("worktree", "add", "-q", str(w), "-b", "side")
    host = HostGitReader(ReaderPool(), PinStore(), tree_checks=True)
    assert host.sync([a.path, w]) == {str(a.path): None, str(w): None}
    return a, w, host


def test_a_declared_linked_worktree_still_reads(declared_wt):
    a, w, host = declared_wt
    assert host.observe(w)["head_ref"] == "refs/heads/side"
    assert host.tree_check(w, "w", principal="p")["changed"] == []
    assert host.observe(a.path)["head_ref"] == "refs/heads/main"
    with Repo(host.pins.get(str(w))) as rp:  # in process (direct mode) too
        assert rp.head_ref() == "refs/heads/side"


def test_a_moved_worktree_is_refused(declared_wt, tmp_path):
    """``git worktree move`` re-points the back-pointer at the new place: the pin of the old path no longer reads."""
    a, w, host = declared_wt
    a.git("worktree", "move", str(w), str(tmp_path.resolve() / "w2"))
    with pytest.raises(Unavailable, match="back-pointer"):
        host.observe(w)
    assert host.observe(a.path)["head_ref"] == "refs/heads/main"


def test_a_back_pointer_symlink_is_refused(declared_wt, tmp_path):
    """The ``gitdir`` file swapped for a symlink to a file with the right content: never followed."""
    a, w, host = declared_wt
    back = os.path.join(host.pins.get(str(w)).gitdir.path, "gitdir")
    copy = tmp_path.resolve() / "f486-back"
    copy.write_bytes(open(back, "rb").read())
    os.unlink(back)
    os.symlink(copy, back)
    with pytest.raises(Unavailable, match="symlink"):
        host.observe(w)


def test_a_dot_git_symlink_in_the_worktree_is_refused(declared_wt, tmp_path):
    """The worktree's ``.git`` file swapped for a symlink to a file with the same content."""
    a, w, host = declared_wt
    copy = tmp_path.resolve() / "f486-dotgit"
    copy.write_bytes((w / ".git").read_bytes())
    (w / ".git").unlink()
    os.symlink(copy, w / ".git")
    with pytest.raises(Unavailable, match=r"\.git"):
        host.observe(w)
    with pytest.raises(Unavailable, match=r"\.git"):
        host.tree_check(w, "w", principal="p")


# ---------------------------------------------------------------- round 1 of #498: one fd-based .git check everywhere


def _plant(a, name, target_dotgit):
    """An agent that can write A's commondir plants ``A/.git/worktrees/<name>`` whose back-pointer names
    ``target_dotgit``: the pins a compromised batch child would hand out for it."""
    gd = a.gitdir / "worktrees" / name
    gd.mkdir(parents=True)
    (gd / "HEAD").write_text("ref: refs/heads/main\n")
    (gd / "commondir").write_text("../..\n")
    (gd / "gitdir").write_text(f"{target_dotgit}\n")
    st = os.stat(gd)
    a_pins = pin_repo(str(a.path), [str(a.path)])
    from orch.gitread.fs import DirPin

    return DirPin(str(gd), st.st_dev, st.st_ino), a_pins.commondir


def _refused_everywhere(host, path):
    with pytest.raises(Unavailable):
        host.observe(path)
    with pytest.raises(Unavailable):
        host.diff(path, branch="main", default_branch="main")
    with pytest.raises(Unavailable):
        host.tree_check(path, "x", principal="p")


def test_c_a_real_linked_worktree_forged_as_a_planted_worktree_of_another_repo(make_repo, tmp_path):
    """(c): B is a real linked worktree of declared C, so its ``.git`` is a regular file; the forged pins make it A's
    planted worktree, whose back-pointer names ``B/.git``. B's ``.git`` names C's gitdir, not the planted one."""
    a, c = committed(make_repo, "a"), committed(make_repo, "c")
    b = tmp_path.resolve() / "b"
    c.git("worktree", "add", "-q", str(b), "-b", "side")
    gd, cd = _plant(a, "evil", f"{b}/.git")
    forged = RepoPins(pin_repo(str(b), [str(b), str(c.path)]).worktree, gd, cd)
    host = HostGitReader(Forging(str(b), forged), PinStore(), tree_checks=True)
    assert host.sync([a.path, c.path, b]) == {str(a.path): None, str(c.path): None, str(b): None}
    assert host.pins.get(str(b)) == forged  # the premise: stored
    _refused_everywhere(host, b)
    assert host.observe(a.path)["head_ref"] == "refs/heads/main"


def test_d_a_parent_directory_swapped_for_a_symlink_is_refused(make_repo, tmp_path):
    """(d): main repo M under ``pp``; after pinning, ``pp`` becomes a symlink to a directory where ``M/.git`` is a
    regular ``.git`` file naming the planted gitdir. The working copy is opened by the ``O_NOFOLLOW`` walk: refused."""
    a, m = committed(make_repo, "a"), committed(make_repo, "pp/m")
    gd, cd = _plant(a, "evil", f"{m.path}/.git")
    forged = RepoPins(pin_repo(str(m.path), [str(m.path)]).worktree, gd, cd)
    host = HostGitReader(Forging(str(m.path), forged), PinStore(), tree_checks=True)
    assert host.sync([a.path, m.path]) == {str(a.path): None, str(m.path): None}
    base = tmp_path.resolve()
    os.rename(base / "pp", base / "pp-real")
    (base / "pp2" / "m").mkdir(parents=True)
    (base / "pp2" / "m" / ".git").write_text(f"gitdir: {gd.path}\n")
    os.symlink(base / "pp2", base / "pp")
    _refused_everywhere(host, m.path)


def test_a_dot_git_file_changed_after_pinning_refuses_every_operation(declared_wt):
    """Codex P2: the ``.git`` check runs for every request in every profile, not only ``tree_check``."""
    a, w, host = declared_wt
    other = a.gitdir / "worktrees" / "elsewhere"
    (w / ".git").write_text(f"gitdir: {other}\n")
    for op in (
        lambda: host.observe(w),
        lambda: host.diff(w, branch="side", default_branch="main"),
        lambda: host.tree_check(w, "w", principal="p"),
    ):
        with pytest.raises(Unavailable, match="does not name the pinned gitdir"):
            op()
