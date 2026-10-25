"""Test helpers for the agent user's read-only share (H1 L5): an in-memory stand-in for the kernel's POSIX ACL xattrs,
a real throwaway git repository with a secret in it, and a share folder shaped like the one root makes."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host import sharemirror
from orch.host.sharemirror import Share

AGENT_UID = 54321
ACCESS, DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
TOKEN = "ghp_SECRETTOKENVALUE123456"


class FakeKernel:
    """``system.posix_acl_*`` per inode in a dict (the tests run on any host; the Linux-root CI test uses the real
    kernel). Records every set, so a test can prove nothing outside the share was ever touched."""

    def __init__(self, monkeypatch, *, drop_sets: bool = False) -> None:
        self.store: dict[tuple[int, int, str], bytes] = {}
        self.sets: list[tuple[int, int, str]] = []
        self.drop_sets = drop_sets
        monkeypatch.setattr(sharemirror, "_getx", self.get)
        monkeypatch.setattr(sharemirror, "_setx", self.set)

    @staticmethod
    def _key(fd: int, name: str) -> tuple[int, int, str]:
        st = os.fstat(fd)
        return st.st_dev, st.st_ino, name

    def get(self, fd: int, name: str) -> bytes | None:
        return self.store.get(self._key(fd, name))

    def set(self, fd: int, name: str, value: bytes) -> None:
        self.sets.append(self._key(fd, name))
        if not self.drop_sets:
            self.store[self._key(fd, name)] = value

    def agent_view(self, path: str | Path, agent_uid: int) -> int:
        """What the agent gets (rwx bits 0-7) on ``path`` the way Linux works it out. An object with an access ACL set
        explicitly: the agent's entry cut by the mask. One made inside a folder with a default ACL: that default's
        ``r-x`` cut by the GROUP bits of the mode it was created with (the umask is ignored there, the create mode is
        not). Run the test with umask 0, so the modes are the ones git and orch asked for."""
        import struct

        st = os.stat(path)
        blob = self.store.get((st.st_dev, st.st_ino, ACCESS))
        if blob is not None:
            entries = [struct.unpack_from("<HHI", blob, 4 + 8 * i) for i in range((len(blob) - 4) // 8)]
            mask = next(p for t, p, _ in entries if t == 0x10)
            return next((p for t, p, i in entries if t == 2 and i == agent_uid), 0) & mask
        return 0o5 & ((st.st_mode >> 3) & 7)

    def of(self, path: str | Path, name: str = ACCESS) -> bytes | None:
        st = os.stat(path)
        return self.store.get((st.st_dev, st.st_ino, name))


def git(repo: Path | str, *args: str, check: bool = True, home: Path | None = None) -> str:
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home or Path(repo).parent), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }  # fmt: skip
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=False)
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


def make_repo(home: Path, name: str = "proj") -> Path:
    """A repository with two commits on ``main``, a tag, a token in its own config, an untracked ``.env`` and a
    ``git stash -u`` that holds a second untracked secret."""
    repo = home / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "first")
    git(repo, "tag", "v1")
    (repo / "a.txt").write_text("two\n")
    git(repo, "commit", "-q", "-am", "second")
    git(repo, "remote", "add", "origin", f"https://x:{TOKEN}@example.invalid/p.git")
    (repo / ".env").write_text("API_KEY=plain-env-secret\n")
    (repo / "stashed.env").write_text("STASHED=stash-only-secret\n")
    git(repo, "stash", "push", "-u", "-q", "--", "stashed.env")
    return repo


def make_share(tmp: Path, monkeypatch, *, drop_sets: bool = False) -> SimpleNamespace:
    """``tmp/share/ann`` (0700, the person's, like root's step leaves it), a state folder, a home with a repository, and
    the fake kernel. The umask is 077 for the test: what git creates is private until an ACL says otherwise."""
    old = os.umask(0o077)
    base = tmp.resolve()
    root = base / "share" / "ann"
    root.mkdir(parents=True, mode=0o700)
    state = base / "state"
    state.mkdir(mode=0o700)
    home = base / "home"
    home.mkdir()
    kernel = FakeKernel(monkeypatch, drop_sets=drop_sets)
    ns = SimpleNamespace(
        base=base, root=root, state=state, home=home, k=kernel, umask=old, ws8="0123abcd",
        share=Share("ann", str(root), str(state), AGENT_UID),
    )  # fmt: skip
    ns.repo = make_repo(home)
    return ns


def restore(ns: SimpleNamespace) -> None:
    os.umask(ns.umask)
    for path in sorted(ns.base.rglob("*"), key=lambda p: len(p.parts), reverse=True):  # release dirs are read-only
        try:
            if not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.lstat().st_mode) | 0o700)
        except OSError:
            pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    ns = make_share(tmp_path, monkeypatch)
    try:
        yield ns
    finally:
        restore(ns)


def tree_bytes(root: Path) -> bytes:
    """Every byte of every file below ``root`` (for 'this secret is nowhere in it'); unreadable files are an error."""
    out = b""
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out += path.read_bytes()
    return out


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def eacces(*_a, **_k):  # pragma: no cover - used to build failing fakes
    raise OSError(errno.EACCES, "denied")
