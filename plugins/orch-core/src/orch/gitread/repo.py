"""One opened repository, under its pins: config, extensions, refs, objects, commits, trees and the merge base.

Opening checks, every time (F7):

* the gitdir and the ``commondir`` are still the pinned directories (device and inode), and the gitdir's
  ``commondir`` file (if any) still names the pinned ``commondir``;
* no ``reftable/`` directory and an allowed config (:mod:`orch.gitread.config`);
* ``objects/`` is a real directory (a symlinked ``objects/`` is refused by ``O_NOFOLLOW``).

``info/grafts``, ``shallow``, ``objects/info/alternates`` and ``refs/replace/`` are never read.
"""

from __future__ import annotations

import os
import re
import stat

from . import limits
from .config import RepoConfig, read_config
from .errors import Unavailable
from .fs import Dir, RepoPins, open_pin
from .objects import HEXLEN, RAWLEN, ObjectStore
from .refs import RefStore, read_head

__all__ = ["Commit", "Repo", "TreeEntry", "canon_mode"]

_COMMITTER = re.compile(rb"committer .*> (\d{1,19}) [+-]\d{4}")


def canon_mode(mode: int) -> int:
    """git's ``canon_mode``: a regular file is 100644 or 100755, then symlink, tree, else gitlink."""
    if stat.S_ISREG(mode):
        return 0o100755 if mode & 0o100 else 0o100644
    if stat.S_ISLNK(mode):
        return 0o120000
    if stat.S_ISDIR(mode):
        return 0o040000
    return 0o160000


class Commit:
    __slots__ = ("oid", "tree", "parents", "time")

    def __init__(self, oid: str, tree: str, parents: list[str], time: int) -> None:
        self.oid = oid
        self.tree = tree
        self.parents = parents
        self.time = time


class TreeEntry:
    __slots__ = ("mode", "name", "oid")

    def __init__(self, mode: int, name: bytes, oid: str) -> None:
        self.mode = mode
        self.name = name
        self.oid = oid

    @property
    def is_tree(self) -> bool:
        return self.mode == 0o040000


class Repo:
    def __init__(self, pins: RepoPins) -> None:
        self.pins = pins
        self._dirs: list[Dir] = []
        try:
            self.gitdir = self._keep(open_pin(pins.gitdir))
            same = pins.commondir == pins.gitdir
            self.commondir = self.gitdir if same else self._keep(open_pin(pins.commondir))
            named = self.gitdir.read_optional("commondir", limits.SMALL_FILE_MAX)
            if same and named is not None:
                raise Unavailable("the gitdir names a commondir that was not pinned")
            if not same:
                if named is None:
                    raise Unavailable("the gitdir no longer names its pinned commondir")
                target = resolve_named(pins.gitdir.path, named)
                if target != pins.commondir.path:
                    raise Unavailable("the gitdir names another commondir than the pinned one")
            if self.commondir.kind("reftable") is not None:
                raise Unavailable("the reftable ref storage is not supported")
            self.config: RepoConfig = read_config(self.commondir.read("config", limits.CONFIG_MAX))
            self.algo = self.config.object_format
            self.hexlen = HEXLEN[self.algo]
            self.rawlen = RAWLEN[self.algo]
            self.objects = ObjectStore(self._keep(self.commondir.sub("objects")), self.algo)
        except BaseException:
            self.close()
            raise
        self._refs: RefStore | None = None
        self._commits: dict[str, Commit] = {}

    def _keep(self, d: Dir) -> Dir:
        self._dirs.append(d)
        return d

    def close(self) -> None:
        objects = getattr(self, "objects", None)
        if objects is not None:
            objects.close()
        for d in self._dirs:
            d.close()
        self._dirs.clear()

    def __enter__(self) -> Repo:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- refs
    @property
    def refs(self) -> RefStore:
        if self._refs is None:
            self._refs = RefStore(self.commondir, self.hexlen)
        return self._refs

    def head_ref(self) -> str:
        return read_head(self.gitdir)

    def resolve_commit(self, ref: str) -> str | None:
        """The commit ``ref`` names (annotated tags peeled), ``None`` when the ref does not exist."""
        oid = self.refs.resolve(ref)
        if oid is None:
            return None
        for _ in range(10):
            kind, data = self.objects.read(oid)
            if kind == "commit":
                return oid
            if kind != "tag":
                raise Unavailable(f"{ref} names a {kind}, not a commit")
            m = re.match(rb"object ([0-9a-f]+)\n", data)
            if not m or len(m.group(1)) != self.hexlen:
                raise Unavailable(f"a malformed tag object {oid}")
            oid = m.group(1).decode()
        raise Unavailable(f"{ref}: too many nested tags")

    # -- objects
    def commit(self, oid: str) -> Commit:
        c = self._commits.get(oid)
        if c is not None:
            return c
        if len(self._commits) >= limits.COMMITS_MAX:
            raise Unavailable("too many commits to walk")
        _, data = self.objects.read(oid, "commit")
        head, _, _ = data.partition(b"\n\n")
        lines = head.split(b"\n")
        if not lines or not lines[0].startswith(b"tree "):
            raise Unavailable(f"commit {oid} has no tree")
        tree = lines[0][5:].decode("ascii", errors="replace")
        parents: list[str] = []
        time = 0
        for ln in lines[1:]:
            if ln.startswith(b"parent "):
                parents.append(ln[7:].decode("ascii", errors="replace"))
            elif ln.startswith(b"committer "):
                m = _COMMITTER.fullmatch(ln)
                if m:
                    time = int(m.group(1))
        for h in (tree, *parents):
            if len(h) != self.hexlen or not all(ch in "0123456789abcdef" for ch in h):
                raise Unavailable(f"commit {oid} is malformed")
        c = Commit(oid, tree, parents, time)
        self._commits[oid] = c
        return c

    def tree(self, oid: str, max_entries: int = limits.TREE_ENTRIES_MAX) -> list[TreeEntry]:
        """The entries of a tree; more than ``max_entries`` is refused while parsing (before they are built)."""
        _, data = self.objects.read(oid, "tree")
        out: list[TreeEntry] = []
        i = 0
        n = len(data)
        r = self.rawlen
        while i < n:
            sp = data.find(b" ", i, i + 8)
            if sp < 0:
                raise Unavailable(f"tree {oid} is malformed")
            mode_b = data[i:sp]
            if not mode_b or not all(0x30 <= c <= 0x37 for c in mode_b):
                raise Unavailable(f"tree {oid} has a malformed mode")
            nul = data.find(b"\0", sp + 1)
            if nul < 0 or nul + 1 + r > n:
                raise Unavailable(f"tree {oid} is truncated")
            name = data[sp + 1 : nul]
            if not name or b"/" in name or name in (b".", b".."):
                raise Unavailable(f"tree {oid} has a bad entry name")
            if len(out) >= max_entries:
                raise Unavailable("too many paths to compare")
            out.append(TreeEntry(canon_mode(int(mode_b, 8)), name, data[nul + 1 : nul + 1 + r].hex()))
            i = nul + 1 + r
        return out

    def blob(self, oid: str) -> bytes:
        return self.objects.read(oid, "blob")[1]

    # -- merge base
    def merge_base(self, a: str, b: str) -> str | None:
        """The best common ancestor of ``a`` and ``b``. Of several, the one with the newest committer time, then the
        smallest id (``git diff A...B`` uses the first one git finds; with one merge base the two agree)."""
        anc_a = self._ancestors([a])
        if b in anc_a:
            return b
        common: set[str] = set()
        seen = {b}
        todo = [b]
        while todo:
            cur = todo.pop()
            if cur in anc_a:
                common.add(cur)
                continue
            for p in self.commit(cur).parents:
                if p not in seen:
                    seen.add(p)
                    todo.append(p)
        if not common:
            return None
        below = self._ancestors([p for c in common for p in self.commit(c).parents])
        best = [c for c in common if c not in below]
        best.sort(key=lambda c: (-self.commit(c).time, c))
        return best[0]

    def _ancestors(self, starts: list[str]) -> set[str]:
        seen = set(starts)
        todo = list(starts)
        while todo:
            cur = todo.pop()
            for p in self.commit(cur).parents:
                if p not in seen:
                    seen.add(p)
                    todo.append(p)
        return seen


def resolve_named(base: str, named: bytes) -> str:
    """A path written in a gitdir's ``commondir`` or a ``.git`` file, relative to ``base``, as a real path."""
    text = named.decode("utf-8", errors="strict") if named.isascii() or _utf8(named) else None
    if text is None:
        raise Unavailable("a path that is not UTF-8")
    text = text.rstrip("\n").rstrip("\r")
    if not text or "\n" in text or "\0" in text:
        raise Unavailable("a malformed path file")
    path = text if text.startswith("/") else os.path.join(base, text)
    return os.path.realpath(path)


def _utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
