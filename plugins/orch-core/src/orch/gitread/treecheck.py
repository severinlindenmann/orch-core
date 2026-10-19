"""``tree.check`` in the reader (H10, D-P2-19 "the D58 uncommitted-changes check", F2, N3): is a working tree clean
relative to its ``HEAD``? Read with orch's own code, **never a git process**, under the ``worktree`` profile.

What it compares:

* **The index with ``HEAD``** (staged changes): ``.git/index`` v2 to v4 is parsed here; every stage-0 entry must
  have the mode and blob id of ``HEAD``'s tree, and every path of ``HEAD`` must be in the index. An unmerged entry
  (stage 1 to 3) or an ``intent-to-add`` entry is a change.
* **The worktree with the index**: each tracked path is ``lstat``-ed relative to the pinned worktree fd. Where the
  stat data match the entry (and the entry is not racy, git's rule) it is clean; elsewhere the raw bytes are hashed
  as a blob (a symlink: its target string) and compared with the entry's id. A file that a git filter would show as
  unchanged counts as changed (conservative). Any other file type (FIFO, socket, device) at a tracked path is
  ``why: "type"`` and is never opened.
* **Untracked files**: the worktree is walked minus the ``.gitignore`` files and ``info/exclude`` (git's pattern
  rules, matched by :mod:`orch.gitread.wildmatch` without backtracking; ``core.excludesFile`` is config and is not
  read here, so a file only it ignores counts as untracked. In direct mode the caller may pass the person's own global
  ignore file as data, ``excludes``, at the lowest precedence: :mod:`orch.gitread.local`, #565).
* **Flags**: ``skip-worktree`` and ``assume-unchanged`` (``assume-valid``) entries are reported as ``why: "flag"``.

No config key other than the reader's three, and no attributes, filter, ``core.fsmonitor``, hook or
``core.hooksPath`` is read; the index's ``FSMN`` (fsmonitor) and ``UNTR`` (untracked cache) extensions are skipped
unread, so nothing in the repository can make this spawn a process or skip a file.

**Unknown, never a guess.** Whatever cannot be established safely raises
:class:`~orch.gitread.errors.Unavailable` and the caller fails closed: a malformed or split index, an unsupported
required index extension, a populated submodule (its own ``HEAD`` is not checked here), a tree deeper than
:data:`MAX_DEPTH`, more than :data:`~orch.gitread.limits.INDEX_ENTRIES` index entries or an index over
:data:`~orch.gitread.limits.INDEX_MAX`, more than :data:`~orch.gitread.limits.WALK_ENTRIES` worktree entries, a
direct-mode check past its work budget (:class:`~orch.gitread.wildmatch.Budget`), a
detached or unreadable ``HEAD``.

The answer is raw (paths as base64 of their bytes); :mod:`orch.gitread.treejob` turns it into the escaped
``TreeCheckResult`` of section 0.8.2.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import stat
import struct
from typing import Any

from . import limits
from .errors import Unavailable
from .fs import Dir
from .ignorepolicy import IgnorePolicy, Ignores
from .repo import Repo, canon_mode
from .wildmatch import Budget

__all__ = ["MAX_CHANGED", "MAX_DEPTH", "check", "parse_index"]

#: entries in ``changed`` (section 0.8.2: ``{0..200}``); the walk stops at one more and says ``more``
MAX_CHANGED = 200
#: directory levels walked (each holds an fd; the child has 64)
MAX_DEPTH = 32
#: raw path bytes returned per entry (section 0.8.2: ``path`` is at most 4096)
PATH_BYTES = 4096

_MODES = {0o100644, 0o100755, 0o120000, 0o160000}
_U32 = 0xFFFFFFFF


class _Full(Exception):
    """More than :data:`MAX_CHANGED` changes found: the answer is dirty, the rest need not be read."""


class Entry:
    __slots__ = (
        "path",
        "mode",
        "oid",
        "ctime_s",
        "ctime_ns",
        "mtime_s",
        "mtime_ns",
        "ino",
        "uid",
        "gid",
        "size",
        "stage",
        "assume_valid",
        "skip_worktree",
        "intent_to_add",
    )

    def __init__(self, path: bytes, fields: tuple[int, ...], oid: str, flags: int, ext: int) -> None:
        self.path = path
        (self.ctime_s, self.ctime_ns, self.mtime_s, self.mtime_ns, _dev, self.ino, self.mode, self.uid, self.gid,
         self.size) = fields  # fmt: skip
        self.oid = oid
        self.stage = (flags >> 12) & 3
        self.assume_valid = bool(flags & 0x8000)
        self.skip_worktree = bool(ext & 0x4000)
        self.intent_to_add = bool(ext & 0x2000)


def _varint(data: bytes, i: int) -> tuple[int, int]:
    """git's offset varint (index v4 path prefix lengths)."""
    if i >= len(data):
        raise Unavailable("the index is truncated")
    c = data[i]
    i += 1
    val = c & 0x7F
    while c & 0x80:
        if i >= len(data) or val > (1 << 32):
            raise Unavailable("the index is malformed")
        val += 1
        c = data[i]
        i += 1
        val = (val << 7) + (c & 0x7F)
    return val, i


def _check_name(name: bytes) -> None:
    if not name or len(name) > 65535 or b"\0" in name or name.startswith(b"/") or name.endswith(b"/"):
        raise Unavailable("the index names a malformed path")
    for comp in name.split(b"/"):
        if comp in (b"", b".", b"..") or comp.lower() == b".git":
            raise Unavailable("the index names a malformed path")


def _header(data: bytes, algo: str) -> tuple[int, int, bytes]:
    """``(version, entry count, body)`` of an index file, its size, header and trailer checked. The checksum guards
    against corruption only: the index is agent-writable, and an agent can recompute it (or write git's
    ``index.skipHash`` zero trailer, which skips it)."""
    rawlen = 20 if algo == "sha1" else 32
    if len(data) > limits.INDEX_MAX:
        raise Unavailable(f"the index is larger than {limits.INDEX_MAX >> 20} MiB")
    if len(data) < 12 + rawlen or data[:4] != b"DIRC":
        raise Unavailable("the index is malformed")
    version, count = struct.unpack_from(">II", data, 4)
    if version not in (2, 3, 4):
        raise Unavailable(f"index version {version} is not supported")
    if count > limits.INDEX_ENTRIES:
        raise Unavailable(f"the index has more than {limits.INDEX_ENTRIES} entries")
    body, trailer = memoryview(data)[:-rawlen], data[-rawlen:]
    if trailer != b"\0" * rawlen:
        h = hashlib.sha1() if algo == "sha1" else hashlib.sha256()
        h.update(body)
        if h.digest() != trailer:
            raise Unavailable("the index checksum does not match")
    return version, count, bytes(body)


def _name_v4(body: bytes, j: int, prev: bytes) -> tuple[bytes, int]:
    """Index v4: the previous path minus a varint prefix length, plus a NUL-terminated suffix; no padding."""
    strip, j = _varint(body, j)
    if strip > len(prev):
        raise Unavailable("the index is malformed")
    nul = body.find(b"\0", j)
    if nul < 0:
        raise Unavailable("the index is truncated")
    return prev[: len(prev) - strip] + body[j:nul], nul + 1


def _name_v23(body: bytes, i: int, j: int, flags: int) -> tuple[bytes, int]:
    """Index v2/v3: the path (its length in the flags, or NUL-terminated at 0xFFF and over), then NUL padding."""
    nlen = flags & 0xFFF
    if nlen < 0xFFF:
        name = body[j : j + nlen]
        if len(name) != nlen or body[j + nlen : j + nlen + 1] != b"\0":
            raise Unavailable("the index is malformed")
    else:
        nul = body.find(b"\0", j + 0xFFF)
        if nul < 0:
            raise Unavailable("the index is truncated")
        name = body[j:nul]
    # git's ondisk_cache_entry_size: the entry (from its start i) plus the name and at least one NUL, padded to 8
    end = i + (((j - i) + len(name) + 8) & ~7)
    if end > len(body):
        raise Unavailable("the index is truncated")
    return name, end


def _check_extensions(body: bytes, i: int) -> None:
    """The extensions after the entries: an upper-case signature is optional and skipped unread (``TREE``, ``REUC``,
    ``UNTR``, ``FSMN``, ``EOIE``, ``IEOT``, ...); a lower-case one (``link``: split index, ``sdir``: sparse index, or
    anything newer) is required to understand the index, so the answer is unknown."""
    n = len(body)
    while i < n:
        if i + 8 > n:
            raise Unavailable("the index is malformed")
        sig = body[i : i + 4]
        (size,) = struct.unpack_from(">I", body, i + 4)
        if i + 8 + size > n:
            raise Unavailable("the index is truncated")
        if not 0x41 <= sig[0] <= 0x5A:
            raise Unavailable(f"the index extension {sig.decode('ascii', 'replace')!r} is not supported")
        i += 8 + size


def parse_index(data: bytes, algo: str) -> list[Entry]:
    """The entries of an index file (v2, v3, v4), its checksum verified, sorted and well formed. A required
    extension (split or sparse index) or an entry of another mode than a file, symlink or gitlink (a sparse
    directory entry) is refused: unknown."""
    version, count, body = _header(data, algo)
    rawlen = 20 if algo == "sha1" else 32
    n = len(body)
    fixed = 40 + rawlen + 2
    i = 12
    prev = b""
    last: tuple[bytes, int] | None = None
    out: list[Entry] = []
    for _ in range(count):
        if i + fixed > n:
            raise Unavailable("the index is truncated")
        fields = struct.unpack_from(">10I", body, i)
        oid = body[i + 40 : i + 40 + rawlen].hex()
        (flags,) = struct.unpack_from(">H", body, i + 40 + rawlen)
        j = i + fixed
        ext = 0
        if flags & 0x4000:
            if version < 3 or j + 2 > n:
                raise Unavailable("the index is malformed")
            (ext,) = struct.unpack_from(">H", body, j)
            j += 2
        name, j = _name_v4(body, j, prev) if version == 4 else _name_v23(body, i, j, flags)
        _check_name(name)
        entry = Entry(name, fields, oid, flags, ext)
        if entry.mode not in _MODES:
            raise Unavailable(f"the index has an entry of mode {entry.mode:o}")
        key = (name, entry.stage)
        if last is not None and key <= last:
            raise Unavailable("the index is not sorted")
        last = key
        prev = name
        out.append(entry)
        i = j
    _check_extensions(body, i)
    return out


# -- ignore patterns: orch.gitread.ignorepolicy is the one place that decides and applies them (#583 round 3)


# -- the check


class _Check:
    def __init__(
        self,
        repo: Repo,
        worktree: Dir,
        *,
        detached_ok: bool = False,
        policy: IgnorePolicy | None = None,
        budget: Budget | None = None,
        removal: bool = False,
    ) -> None:
        self.repo = repo
        #: a removal check (#583 round 3, A): no ignore rule at all, untracked directories reported whole, with sizes
        self.removal = removal
        #: ``(bytes, files)`` of each untracked directory reported whole (removal checks)
        self.sizes: dict[bytes, tuple[int, int]] = {}
        #: every untracked path with its size, mtime and mode (removal checks): the set a discard may delete
        self.fp = hashlib.sha256()
        #: direct mode (#583, M1/L5): the work allowed (a deadline); the host's child has its own wall clock
        self.budget = budget
        self.worktree = worktree
        #: direct mode (#565): a detached ``HEAD`` is compared like a branch (``head_ref`` ``None``); host mode refuses
        self.detached_ok = detached_ok
        #: which ignore rules apply (orch.gitread.ignorepolicy, the one place that decides); built in run()
        self.policy = policy
        self.algo = repo.algo
        self.changed: dict[bytes, str] = {}
        self.walked = 0
        #: the index file's mtime ``(s, ns)``; ``None``: no index (every entry would be racy)
        self.index_time: tuple[int, int] | None = None
        #: ``HEAD``'s branch ref and commit, read in this same child as the tree (no race with another read)
        self.head_ref: str | None = None
        self.head: str | None = None
        #: every index path (any stage), the stage-0 entries compared with the worktree, their parent directories
        self.tracked: set[bytes] = set()
        self.files: dict[bytes, Entry] = {}
        self.dirs: set[bytes] = set()
        #: tracked paths met by the walk (the others are deleted)
        self.seen: set[bytes] = set()

    def note(self, path: bytes, why: str) -> None:
        if path in self.changed:
            return
        self.changed[path] = why
        if len(self.changed) > MAX_CHANGED:
            raise _Full

    def _blob_id(self, data: bytes) -> str:
        h = hashlib.sha1() if self.algo == "sha1" else hashlib.sha256()
        h.update(b"blob %d\0" % len(data))
        h.update(data)
        return h.hexdigest()

    # index and HEAD
    def read_index(self) -> tuple[list[Entry], tuple[int, int] | None]:
        gd = self.repo.gitdir
        if gd.kind("index") is None:
            return [], None
        fd, st = gd.open_file("index")
        try:
            if st.st_size > limits.INDEX_MAX:
                raise Unavailable(f"the index is larger than {limits.INDEX_MAX >> 20} MiB")
            chunks, total = [], 0
            while True:
                b = os.read(fd, min(1 << 20, limits.INDEX_MAX + 1 - total))
                if not b:
                    break
                chunks.append(b)
                total += len(b)
                if total > limits.INDEX_MAX:
                    raise Unavailable(f"the index is larger than {limits.INDEX_MAX >> 20} MiB")
        finally:
            os.close(fd)
        when = (st.st_mtime_ns // 1_000_000_000, st.st_mtime_ns % 1_000_000_000)
        return parse_index(b"".join(chunks), self.algo), when

    def head_tree(self) -> dict[bytes, tuple[int, str]]:
        """``HEAD``'s paths; sets :attr:`head_ref` and :attr:`head` (``None`` on an unborn branch: the empty tree)."""
        if self.detached_ok:
            from .refs import read_head_any

            ref, detached = read_head_any(self.repo.gitdir, self.repo.hexlen)  # refuses a malformed HEAD
            self.head_ref = ref
            if detached is not None:
                self.repo.commit(detached)  # a detached HEAD must name a commit (missing or another type: unknown)
            sha = detached if ref is None else self.repo.resolve_commit(ref)
            self.head = sha
        else:
            self.head_ref = ref = self.repo.head_ref()  # refuses a detached or malformed HEAD
            self.head = sha = self.repo.resolve_commit(ref)
        if sha is None:
            return {}
        out: dict[bytes, tuple[int, str]] = {}
        todo = [(b"", self.repo.commit(sha).tree)]
        while todo:
            prefix, oid = todo.pop()
            for e in self.repo.tree(oid):
                p = prefix + e.name
                if e.is_tree:
                    todo.append((p + b"/", e.oid))
                else:
                    out[p] = (e.mode, e.oid)
                    if len(out) > limits.TREE_ENTRIES_MAX:
                        raise Unavailable("HEAD has too many paths to compare")
        return out

    def compare_head(self, entries: list[Entry], head: dict[bytes, tuple[int, str]]) -> None:
        in_index: set[bytes] = set()
        for e in entries:
            in_index.add(e.path)
            if e.stage:
                self.note(e.path, "modified")  # unmerged
                continue
            if e.skip_worktree or e.assume_valid:
                self.note(e.path, "flag")
            if e.intent_to_add:
                self.note(e.path, "modified")
                continue
            h = head.get(e.path)
            if h is None or h != (canon_mode(e.mode), e.oid):
                self.note(e.path, "modified")
        for p in head:
            if p not in in_index:
                self.note(p, "deleted")

    # worktree
    @staticmethod
    def _stat_same(e: Entry, st: os.stat_result) -> bool:
        m_s, m_ns = divmod(st.st_mtime_ns, 1_000_000_000)
        c_s, c_ns = divmod(st.st_ctime_ns, 1_000_000_000)
        if e.mtime_s != m_s & _U32 or e.ctime_s != c_s & _U32:
            return False
        if (e.mtime_ns and e.mtime_ns != m_ns) or (e.ctime_ns and e.ctime_ns != c_ns):
            return False  # an index written without nanoseconds compares seconds only (git's USE_NSEC)
        return (
            e.size == st.st_size & _U32
            and e.ino == st.st_ino & _U32
            and e.uid == st.st_uid & _U32
            and e.gid == st.st_gid & _U32
        )

    def _racy(self, e: Entry, st: os.stat_result) -> bool:
        """When the stat data cannot be trusted and the bytes are hashed. Stricter than git's racy-clean rule, at
        **second** granularity whatever the nanoseconds say: the entry's mtime, or the file's own mtime or ctime, in
        or after the second the index was written (a write in that second may keep size and mtime). An index whose
        own mtime cannot be read trusts nothing."""
        if self.index_time is None:
            return True
        sec = self.index_time[0]
        return (
            e.mtime_s >= sec & _U32 or st.st_mtime_ns // 1_000_000_000 >= sec or st.st_ctime_ns // 1_000_000_000 >= sec
        )

    def _hash_file(self, d: Dir, name: str, st: os.stat_result) -> str | None:
        """The blob id of a regular file's raw bytes; ``None`` when it changed while it was read."""
        try:
            fd, fst = d.open_file(name)  # O_NOFOLLOW | O_NONBLOCK, regular files only
        except Unavailable:
            return None
        try:
            if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                return None
            size = fst.st_size
            h = hashlib.sha1() if self.algo == "sha1" else hashlib.sha256()
            h.update(b"blob %d\0" % size)
            got = 0
            while True:
                b = os.read(fd, 1 << 20)
                if not b:
                    break
                got += len(b)
                if got > size:
                    return None
                h.update(b)
            return h.hexdigest() if got == size else None
        finally:
            os.close(fd)

    def compare_entry(self, d: Dir, name: str, st: os.stat_result, e: Entry) -> None:
        mode = e.mode
        if mode == 0o160000:  # a submodule: an empty (not checked out) directory is clean, as git sees it
            if not stat.S_ISDIR(st.st_mode):
                self.note(e.path, "type")
                return
            sub = d.sub(name)
            try:
                if sub.listdir():
                    raise Unavailable("a checked-out submodule's own state is not checked; commit or remove it")
            finally:
                sub.close()
            return
        if mode == 0o120000:
            if not stat.S_ISLNK(st.st_mode):
                self.note(e.path, "type")
                return
            if self._stat_same(e, st) and not self._racy(e, st):
                return
            try:
                target = os.readlink(os.fsencode(name), dir_fd=d.fd)
            except OSError:
                self.note(e.path, "modified")
                return
            if self._blob_id(target) != e.oid:
                self.note(e.path, "modified")
            return
        if not stat.S_ISREG(st.st_mode):
            self.note(e.path, "type")  # a FIFO, socket, device or directory: never opened
            return
        if canon_mode(st.st_mode) != mode:
            self.note(e.path, "modified")  # the executable bit (core.fileMode is config: not read)
            return
        if self._stat_same(e, st) and not self._racy(e, st):
            return
        if self._hash_file(d, name, st) != e.oid:
            self.note(e.path, "modified")

    def walk(self, d: Dir, rel: bytes, parent: Ignores, inside_ignored: bool, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise Unavailable(f"the working tree is deeper than {MAX_DEPTH} directories")
        assert self.policy is not None
        ignores, pushed = self.policy.directory(d, rel, parent)  # the policy reads and judges the .gitignore
        try:
            try:
                with os.scandir(d.fd) as it:
                    names = sorted(entry.name for entry in it)
            except OSError as e:
                why = errno.errorcode.get(e.errno or 0, "error")
                raise Unavailable(f"a directory of the working tree cannot be listed ({why})") from None
            for name in names:
                self._count()
                bname = os.fsencode(name)
                if not rel and bname == b".git":
                    continue
                path = rel + b"/" + bname if rel else bname
                try:
                    st = os.stat(name, dir_fd=d.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # gone since the listing: a tracked path is then "deleted" below
                except OSError:
                    raise Unavailable("a path of the working tree cannot be read") from None
                if path in self.tracked:
                    self.seen.add(path)
                    e = self.files.get(path)
                    if e is not None:
                        self.compare_entry(d, name, st, e)
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if is_dir and path in self.dirs:
                    ign = inside_ignored or ignores.ignored(path, True)
                    sub = d.sub(name)
                    try:
                        self.walk(sub, path, ignores, ign, depth + 1)
                    finally:
                        sub.close()
                    continue
                if inside_ignored or ignores.ignored(path, is_dir):
                    continue
                if not is_dir:
                    self._untracked(path, st)
                    continue
                sub = d.sub(name)
                try:
                    if self.removal:  # the whole directory is one entry, with its size (node_modules, .venv)
                        self.sizes[path + b"/"] = self._measure(sub, path, depth + 1)
                        self.note(path + b"/", "untracked")
                        continue
                    if sub.kind(".git") is not None:  # a nested repository: git shows the directory, untracked
                        self.note(path + b"/", "untracked")
                        continue
                    self.walk(sub, path, ignores, False, depth + 1)
                finally:
                    sub.close()
        finally:
            if pushed:
                ignores.levels.pop()

    def _count(self) -> None:
        self.walked += 1
        if self.budget is not None:
            self.budget.spend()
        if self.walked > limits.WALK_ENTRIES:
            raise Unavailable(f"the working tree has more than {limits.WALK_ENTRIES} entries")

    def _untracked(self, path: bytes, st: os.stat_result) -> None:
        if self.removal:
            self.fp.update(b"%s\0%d\0%d\0%o\n" % (path, st.st_size, st.st_mtime_ns, st.st_mode))
        self.note(path, "untracked")

    def _measure(self, d: Dir, rel: bytes, depth: int) -> tuple[int, int]:
        """``(bytes, files)`` below an untracked directory (removal checks), every entry also in :attr:`fp`;
        symlinks are counted, never followed."""
        if depth > MAX_DEPTH:
            raise Unavailable(f"the working tree is deeper than {MAX_DEPTH} directories")
        if self.budget is not None:
            self.budget.check()
        total = files = 0
        try:
            with os.scandir(d.fd) as it:
                names = sorted(entry.name for entry in it)
        except OSError:
            raise Unavailable("a directory of the working tree cannot be listed") from None
        for name in names:
            self._count()
            path = rel + b"/" + os.fsencode(name)
            try:
                st = os.stat(name, dir_fd=d.fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                raise Unavailable("a path of the working tree cannot be read") from None
            self.fp.update(b"%s\0%d\0%d\0%o\n" % (path, st.st_size, st.st_mtime_ns, st.st_mode))
            if stat.S_ISDIR(st.st_mode):
                sub = d.sub(name)
                try:
                    b, f = self._measure(sub, path, depth + 1)
                finally:
                    sub.close()
                total, files = total + b, files + f
            else:
                total, files = total + st.st_size, files + 1
        return total, files

    def _index_sets(self, entries: list[Entry]) -> None:
        self.tracked = {e.path for e in entries}
        self.files = {e.path: e for e in entries if e.stage == 0 and not (e.skip_worktree or e.assume_valid)}
        for e in entries:
            p = e.path
            while True:
                cut = p.rfind(b"/")
                if cut < 0:
                    break
                p = p[:cut]
                if p in self.dirs:
                    break
                self.dirs.add(p)

    def _walk_all(self) -> None:
        if self.policy is None:  # the host's child: the person's configuration cannot be read here
            self.policy = (
                IgnorePolicy.removal(self.budget) if self.removal else IgnorePolicy.status(self.repo, None, self.budget)
            )
        # git's order: core.excludesFile, then info/exclude, then .gitignore files, as far as the policy is certain
        self.walk(self.worktree, b"", self.policy.top(self.repo.commondir), False, 0)
        for p in self.files:
            if p not in self.seen:
                self.note(p, "deleted")

    def run(self) -> dict[str, Any]:
        more = False
        entries, self.index_time = self.read_index()
        head = self.head_tree()  # before any change is noted: a full answer always names its HEAD
        try:
            self.compare_head(entries, head)
            del head
            self._index_sets(entries)
            del entries
            self._walk_all()
        except _Full:
            more = True
        changed = sorted(self.changed.items())
        if len(changed) > MAX_CHANGED:
            changed, more = changed[:MAX_CHANGED], True
        out: dict[str, Any] = {
            "head": self.head,
            "head_ref": self.head_ref,
            "changed": [
                {"path_b64": base64.b64encode(p[:PATH_BYTES]).decode("ascii"), "cut": len(p) > PATH_BYTES, "why": w}
                for p, w in changed
            ],
            "more": more,
        }
        if self.removal:  # what blocks a removal, with sizes, and the fingerprint of the whole untracked set
            out["sizes"] = [
                {"path_b64": base64.b64encode(p[:PATH_BYTES]).decode("ascii"), "bytes": b, "files": f}
                for p, (b, f) in sorted(self.sizes.items())
            ]
            out["fingerprint"] = self.fp.hexdigest()
            out["ignore_rules"] = "none (a removal never depends on ignore rules)"
        elif self.policy is not None:
            out["ignore_rules"] = "applied" if self.policy.why is None else f"none: {self.policy.why}"
        return out


def check(
    repo: Repo,
    worktree: Dir,
    *,
    detached_ok: bool = False,
    policy: IgnorePolicy | None = None,
    budget: Budget | None = None,
    removal: bool = False,
) -> dict[str, Any]:
    """The raw ``tree.check`` answer for the repository ``repo`` whose pinned working copy is ``worktree``.
    ``detached_ok`` and ``excludes``: direct mode only (:mod:`orch.gitread.local`, #565); the host's ``tree_check``
    request passes neither (a detached ``HEAD`` is unknown there, and no file outside the pins is read)."""
    return _Check(repo, worktree, detached_ok=detached_ok, policy=policy, budget=budget, removal=removal).run()
