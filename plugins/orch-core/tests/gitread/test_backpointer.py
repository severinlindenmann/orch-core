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
