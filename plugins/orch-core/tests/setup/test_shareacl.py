"""H1 L5: the person's own sharing ACLs (setup/shareacl.py).

Most tests run against an in-memory stand-in for the kernel's POSIX ACL xattrs (``FakeKernel``: it validates the
order, turns a plain ACL back into the mode, and sets the mode's group bits to the mask like Linux), so they run on
any host. ``test_real_xattrs_on_linux`` uses the real thing and runs on Linux only (CI fails if xattrs are missing).
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orch.setup import shareacl
from orch.setup.osapply import GROUP_OBJ, MASK, OTHER, USER, USER_OBJ
from orch.setup.shareacl import Agent

ACCESS, DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
AGENT = Agent("orch-agent-t", 54321)
UNDEF = 0xFFFFFFFF


class FakeKernel:
    def __init__(self, monkeypatch, *, drop_sets: bool = False, no_acl: bool = False) -> None:
        self.store: dict[tuple[int, int, str], bytes] = {}
        self.real: set[tuple[int, int]] = set()  # inodes the test made: only those get their mode changed
        self.drop_sets, self.no_acl = drop_sets, no_acl
        monkeypatch.setattr(shareacl, "_getx", self.get)
        monkeypatch.setattr(shareacl, "_setx", self.set)
        monkeypatch.setattr(shareacl, "_removex", self.remove)

    def track(self, root: Path) -> None:
        for p in [root, *root.rglob("*")]:
            st = os.lstat(p)
            self.real.add((st.st_dev, st.st_ino))

    def _key(self, fd: int, name: str):
        st = os.fstat(fd)
        return st.st_dev, st.st_ino, name

    def get(self, fd, name):
        if self.no_acl:
            raise OSError(errno.ENOTSUP, "no acl")
        return self.store.get(self._key(fd, name))

    def set(self, fd, name, value):
        if self.no_acl:
            raise OSError(errno.EOPNOTSUPP, "no acl")
        if self.drop_sets:
            return
        entries = shareacl._parse(value)
        assert entries == sorted(entries, key=lambda e: (e[0], e[2])), "the kernel needs sorted ACLs"
        key = self._key(fd, name)
        st = os.fstat(fd)
        named = [e for e in entries if e[0] in (USER, shareacl.GROUP)]
        if name == ACCESS:
            mode = {t: p for t, p, _ in entries}
            if not named:  # equivalent to the mode: the kernel drops the xattr and sets the bits
                self.store.pop(key, None)
                new = (mode[USER_OBJ] << 6) | (mode[GROUP_OBJ] << 3) | mode[OTHER]
            else:
                self.store[key] = value
                new = (mode[USER_OBJ] << 6) | (mode[MASK] << 3) | mode[OTHER]
            if (st.st_dev, st.st_ino) in self.real:
                os.fchmod(fd, (st.st_mode & ~0o777) | new)
        else:
            self.store[key] = value

    def remove(self, fd, name):
        self.store.pop(self._key(fd, name), None)

    def entries(self, path, name=ACCESS):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            blob = self.get(fd, name)
        finally:
            os.close(fd)
        return None if blob is None else shareacl._parse(blob)


def agent_entry(entries, uid=AGENT.uid):
    return next((p for t, p, i in entries or [] if t == USER and i == uid), None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / "home" / "ann"
    repo = home / "code" / "proj"
    (repo / ".git" / "objects" / "aa").mkdir(parents=True)
    (repo / ".git" / "refs").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    obj = repo / ".git" / "objects" / "aa" / "bbcc"
    obj.write_text("x")
    obj.chmod(0o444)
    (repo / ".git" / "hook.sh").write_text("#!/bin/sh\n")
    (repo / ".git" / "hook.sh").chmod(0o755)
    (repo / ".env").write_text("SECRET=1\n")
    for d in (root / "home", home, home / "code", repo, repo / ".git", repo / ".git" / "objects"):
        d.chmod(0o700 if d in (home, repo) else 0o755)
    k = FakeKernel(monkeypatch)
    k.track(root)

    class W:
        pass

    w = W()
    w.root, w.home, w.repo, w.k = root, home, repo, k
    return w


def share(w, **kw):
    w.k.track(w.root)
    return shareacl.share_repo(str(w.repo), agent=AGENT, home=str(w.home), **kw)


def test_tier_a_is_a_noop(world, monkeypatch):
    called = []
    monkeypatch.setattr(shareacl, "_setx", lambda *a: called.append(a))
    rep = shareacl.share_repo(str(world.repo), agent=None, home=str(world.home))
    assert rep.ok and rep.noop and "tier A" in rep.text() and not called
    monkeypatch.setattr(shareacl.pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(shareacl.sys, "platform", "linux")
    assert shareacl.agent_for("ann") is None
    assert shareacl.agent_for("not a valid name!") is None


def test_share_repo_grants_least_access(world):
    mode_before = {p: p.stat().st_mode for p in (world.home, world.repo / ".env")}
    rep = share(world)
    assert rep.ok, rep.text()
    k = world.k
    for d in (world.home, world.home.parent, world.home / "code", world.repo):
        assert agent_entry(k.entries(d)) == 0o1, d  # traverse only
        assert k.entries(d, DEFAULT) is None
    git = world.repo / ".git"
    for kind in (ACCESS, DEFAULT):
        assert agent_entry(k.entries(git, kind)) == 0o5
        assert agent_entry(k.entries(git / "objects" / "aa", kind)) == 0o5
    assert agent_entry(k.entries(git / "HEAD")) == 0o4  # a file without the owner's x: read only
    assert agent_entry(k.entries(git / "hook.sh")) == 0o5
    assert k.entries(git / "HEAD", DEFAULT) is None
    assert agent_entry(k.entries(world.repo / ".env")) == 0  # the working tree is closed to the agent: u:agent:---
    assert agent_entry(k.entries(world.repo / ".git" / "objects" / "aa" / "bbcc")) == 0o4
    # nothing for group or other changed: the home keeps other = ---, group effective = ---
    ents = {t: p for t, p, _ in k.entries(world.home)}
    assert ents[OTHER] == 0 and ents[GROUP_OBJ] == 0 and ents[MASK] == 0o1
    assert world.home.stat().st_mode & 0o007 == mode_before[world.home] & 0o007
    assert (world.repo / ".env").stat().st_mode == mode_before[world.repo / ".env"]
    # the default ACL copies the folder's bits without write: nothing new is writable by group or other
    d = {t: p for t, p, _ in k.entries(git, DEFAULT)}
    assert d[GROUP_OBJ] == 0 and d[OTHER] == 0 and d[MASK] == 0o5  # umask is not applied: only the agent gains


def test_reapply_changes_nothing(world):
    assert share(world).ok
    snap = dict(world.k.store)
    rep = share(world)
    assert rep.ok and set(rep.counts()) - {"note"} == {"unchanged"}
    assert world.k.store == snap


def test_unshare_restores_the_plain_modes(world):
    modes = {p: p.stat().st_mode for p in [world.home, world.repo, world.repo / ".git", world.repo / ".git" / "HEAD"]}
    assert share(world).ok
    rep = shareacl.unshare(str(world.repo), is_repo=True, agent=AGENT, home=str(world.home))
    assert rep.ok, rep.text()
    assert world.k.store == {}
    for p, m in modes.items():
        assert p.stat().st_mode == m, p


def test_unshare_keeps_ancestors_other_paths_need(world):
    assert share(world).ok
    other = world.home / "code" / "other"
    keep = str(other / ".git")
    rep = shareacl.unshare(str(world.repo), is_repo=True, keep=[keep], agent=AGENT, home=str(world.home))
    assert rep.ok
    assert agent_entry(world.k.entries(world.home / "code")) == 0o1  # kept
    assert agent_entry(world.k.entries(world.home)) == 0o1
    assert agent_entry(world.k.entries(world.repo)) is None
    assert agent_entry(world.k.entries(world.repo / ".git")) is None
    assert any(i.action == "skipped" and "still needed" in i.detail for i in rep.items)


def test_release_tree_and_outside_home(world):
    rel = world.home / "rel"
    (rel / "bin").mkdir(parents=True)
    (rel / "bin" / "orch").write_text("#!/bin/sh\n")
    (rel / "bin" / "orch").chmod(0o755)
    world.k.track(world.root)
    rep = shareacl.share_release(str(rel), agent=AGENT, home=str(world.home))
    assert rep.ok, rep.text()
    assert agent_entry(world.k.entries(rel, DEFAULT)) == 0o5
    assert agent_entry(world.k.entries(rel / "bin" / "orch")) == 0o5
    assert agent_entry(world.k.entries(world.home)) == 0o1
    out = shareacl.share_release(str(world.root / "elsewhere"), agent=AGENT, home=str(world.home))
    assert not out.ok and "outside your home" in out.problem


def test_symlinked_component_is_refused(world):
    real = world.home / "code"
    link = world.home / "linked"
    link.symlink_to(real)
    rep = shareacl.share_repo(str(link / "proj"), agent=AGENT, home=str(world.home))
    assert not rep.ok and "symbolic link" in rep.problem
    assert world.k.store == {}


def test_symlink_inside_git_is_not_followed(world):
    target = world.repo / ".env"
    (world.repo / ".git" / "alias").symlink_to(target)
    assert share(world).ok
    assert agent_entry(world.k.entries(target)) == 0  # closed as a working tree file; the link was not followed


def test_git_that_is_a_file_or_a_link_is_refused(world):
    git = world.repo / ".git"
    for p in sorted(git.rglob("*"), reverse=True):
        p.unlink() if not p.is_dir() else p.rmdir()
    git.rmdir()
    git.write_text("gitdir: ../elsewhere\n")
    rep = share(world)
    assert not rep.ok and ".git" in rep.problem


def test_hard_linked_file_is_skipped(world):
    os.link(world.repo / ".env", world.repo / ".git" / "linked")
    rep = share(world)
    assert rep.ok
    assert agent_entry(world.k.entries(world.repo / ".env")) == 0  # not granted read through the .git link
    assert any(i.action == "skipped" and "hard link" in i.detail for i in rep.items)


def test_foreign_owner_and_traverse_refusals(world):
    # someone else's folders must already be open to traverse; ours-by-uid is what we may change
    rep = shareacl.share_repo(str(world.repo), agent=AGENT, home=str(world.home), euid=os.geteuid() + 1)
    assert not rep.ok and ("belongs to someone else" in rep.problem or "not open to traverse" in rep.problem)
    assert world.k.store == {}


def test_root_and_agent_uid_are_refused(world):
    for euid in (0, AGENT.uid):
        rep = shareacl.share_repo(
            str(world.repo), agent=Agent("orch-agent-t", AGENT.uid), home=str(world.home), euid=euid
        )
        assert not rep.ok and "not as root" in rep.problem


def test_widening_for_someone_else_is_refused(world):
    # bob has rwx but the mask is ---: adding a mask that covers the agent would give bob access
    bob = [(USER_OBJ, 7, UNDEF), (USER, 7, 4242), (GROUP_OBJ, 0, UNDEF), (MASK, 0, UNDEF), (OTHER, 0, UNDEF)]
    fd = os.open(world.home, os.O_RDONLY)
    key = world.k._key(fd, ACCESS)
    os.close(fd)
    world.k.store[key] = shareacl._build(bob)
    rep = share(world)
    assert not rep.ok and "widen" in rep.problem
    assert agent_entry(world.k.entries(world.home)) is None


def test_other_named_entries_are_kept(world):
    carol = [(USER_OBJ, 7, UNDEF), (USER, 5, 4243), (GROUP_OBJ, 0, UNDEF), (MASK, 5, UNDEF), (OTHER, 0, UNDEF)]
    fd = os.open(world.home, os.O_RDONLY)
    key = world.k._key(fd, ACCESS)
    os.close(fd)
    world.k.store[key] = shareacl._build(carol)
    assert share(world).ok
    ents = world.k.entries(world.home)
    assert agent_entry(ents, 4243) == 5 and agent_entry(ents) == 1
    assert shareacl.unshare(str(world.repo), is_repo=True, agent=AGENT, home=str(world.home)).ok
    assert agent_entry(world.k.entries(world.home, ACCESS), 4243) == 5


def test_read_back_mismatch_fails_closed(world, monkeypatch):
    k = FakeKernel(monkeypatch, drop_sets=True)
    k.track(world.root)
    rep = shareacl.share_repo(str(world.repo), agent=AGENT, home=str(world.home))
    assert not rep.ok and "read back differs" in rep.problem


def test_filesystem_without_acls_is_a_plain_refusal(world, monkeypatch):
    FakeKernel(monkeypatch, no_acl=True)
    rep = shareacl.share_repo(str(world.repo), agent=AGENT, home=str(world.home))
    assert not rep.ok and "does not support ACLs" in rep.problem


def test_bad_paths_are_refused(world):
    for bad in ("relative", "/", f"{world.repo}/../x", f"{world.repo}/", f"{world.repo}//x", "/tmp/a\nb"):
        rep = shareacl.share_repo(bad, agent=AGENT, home=str(world.home))
        assert not rep.ok, bad


def test_no_process_is_spawned(world, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("shareacl must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    assert share(world).ok


def test_report_text_lists_what_was_granted(world):
    text = share(world).text()
    assert "traverse" in text and "tree" in text and "Summary:" in text and str(world.repo / ".git") in text


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only: real POSIX ACL xattrs")
def test_real_xattrs_on_linux(tmp_path):
    base = tmp_path.resolve() / "home" / "ann"
    repo = base / "proj"
    (repo / ".git" / "objects").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: x\n")
    (repo / ".env").write_text("S=1\n")
    for d in (base, repo, repo / ".git", repo / ".git" / "objects"):
        d.chmod(0o700)
    uid = 61234  # the kernel does not need the user to exist
    agent = Agent("orch-agent-t", uid)
    try:
        os.setxattr(base, ACCESS, shareacl._build([(USER_OBJ, 7, UNDEF), (GROUP_OBJ, 0, UNDEF), (OTHER, 0, UNDEF)]))
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must support POSIX ACL xattrs, but: {exc}")
        pytest.skip(f"no POSIX ACL support here: {exc}")
    rep = shareacl.share_repo(str(repo), agent=agent, home=str(base))
    assert rep.ok, rep.text()
    for d in (base, repo):
        ents = shareacl._parse(os.getxattr(d, ACCESS))
        assert agent_entry(ents, uid) == 1
        assert {t: p for t, p, _ in ents}[OTHER] == 0
    git = repo / ".git"
    assert agent_entry(shareacl._parse(os.getxattr(git, DEFAULT)), uid) == 5
    assert agent_entry(shareacl._parse(os.getxattr(git / "HEAD", ACCESS)), uid) == 4
    with pytest.raises(OSError):
        os.getxattr(repo / ".env", ACCESS)
    assert set(shareacl.share_repo(str(repo), agent=agent, home=str(base)).counts()) - {"note"} == {"unchanged"}
    assert shareacl.unshare(str(repo), is_repo=True, agent=agent, home=str(base)).ok
    for d in (base, repo, git):
        assert d.stat().st_mode & 0o777 == 0o700
        with pytest.raises(OSError):
            os.getxattr(d, DEFAULT)


def test_cli_registration_hook_and_refusals(world, monkeypatch, capsys):
    import io

    from orch.cli import share as cli

    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(shareacl, "agent_for", lambda person=None: None)  # tier A
    assert cli.main(["repo", str(world.repo)], env={}, out=out, err=err) == 0
    assert "tier A" in out.getvalue()
    assert cli.main(["repo", str(world.repo)], env={"ORCH_GRANT": "x"}, out=out, err=err) == cli.EXIT_REFUSED
    assert "an agent never" in err.getvalue()
    monkeypatch.setattr(shareacl, "agent_for", lambda person=None: AGENT)
    monkeypatch.setattr(shareacl, "_home_of_euid", lambda: str(world.home))
    world.k.track(world.root)
    out = io.StringIO()
    assert cli.share_registered_repo(str(world.repo)).ok
    assert cli.main(["remove", str(world.repo)], env={}, out=out, err=err) == 0
    assert "removed" in out.getvalue()
    assert cli.main(["repo", "/nowhere/x"], env={}, out=io.StringIO(), err=err) == cli.EXIT_REFUSED


def test_secrets_and_working_tree_are_closed_and_exposure_is_reported(world):
    home, repo = world.home, world.repo
    (home / ".netrc").write_text("machine x password y\n")
    (home / ".netrc").chmod(0o644)
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("k\n")
    (home / ".aws" / "credentials").chmod(0o644)
    (home / ".ssh").mkdir()
    (home / "notes.txt").write_text("n\n")
    (home / "notes.txt").chmod(0o644)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a\n")
    rep = share(world)
    assert rep.ok, rep.text()
    k = world.k
    for p in (home / ".netrc", home / ".aws" / "credentials", home / ".ssh", repo / ".env", repo / "src"):
        assert agent_entry(k.entries(p)) == 0, p
    assert agent_entry(k.entries(repo / ".git")) == 0o5  # .git stays open
    text = rep.text()
    assert "notes.txt" in text and "can read" in text  # other-readable and not closed: reported
    assert ".netrc" not in next(i.detail for i in rep.items if i.what == "exposed" and i.path == str(home))
    assert shareacl.unshare(str(repo), is_repo=True, agent=AGENT, home=str(home)).ok
    assert k.store == {}
