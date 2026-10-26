"""The pinned workspace root: the one way the store (and what reads the workspace through it) touches a file under
the root (#516).

**Invariant.** After discovery, every open, stat, listing, rename and unlink under the workspace root goes through a
:class:`RootFS`: the root directory is opened **once** (``O_NOFOLLOW`` on its last component; its ancestors are the
trusted start discovery resolved) and checked against the identity discovery saw (``st_dev``, ``st_ino``); everything
below it is reached **relative to that fd, one component at a time, with ``O_NOFOLLOW``**. There is no path lookup
between a check and a read, and no symlink inside the workspace is ever followed: a directory component that is a
symlink refuses (``validation.path``); a final component that is a symlink is what the old path helpers made it (a read
sees it as absent, an open refuses with ``ELOOP``, a rename replaces the link itself, never its target).

A path is named either relative to the root (``"tickets/<uid>/events.jsonl"``) or as the absolute path the store
builds from :attr:`RootFS.path` (``root / "tickets" / ...``): the latter is turned into components **lexically** (the
same components as the root, then the rest), never resolved. ``..``, ``.``, empty components and NUL refuse.

**Fds.** One root fd per :class:`RootFS` (one per store), held until :meth:`close` (or a ``weakref.finalize``), plus
at most :data:`CACHED` (three) top-level directory fds (``tickets``, ``events``, ``.state``: every hot path starts with
one of them). A cached fd is re-checked by :meth:`revalidate` (the store calls it each time it takes its lock): when
the name below the root is no longer that directory (swapped for a symlink, replaced, renamed away) the fd is dropped
and the next access walks again (and refuses a symlink). Every other fd is opened for one call and closed before it
returns, so a call holds at most ``depth + 1`` more fds and nothing grows with the number of tickets. The fd cache is
**not thread-safe**: each store (and so its RootFS) is used from one thread at a time (the host runs a workspace on its
single-thread executor; direct mode is one process, one command).

POSIX only (``dir_fd`` and ``O_NOFOLLOW``), like the rest of the store's hardening.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
import weakref
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from . import fsio
from .errors import StoreError

#: the top-level directories whose fds a RootFS keeps (bounded: one fd each)
CACHED = frozenset({"tickets", "events", ".state"})

__all__ = ["MAX_DEPTH", "MAX_READ", "MAX_RM_DEPTH", "RootFS", "rm_at"]

MAX_READ = 16 * 1024 * 1024
#: the deepest path below the root a call may name (the store's deepest is ``tickets/<uid>/artifacts/<name>``)
MAX_DEPTH = 16
#: the deepest tree :func:`rm_at` removes: it holds one fd per level, so an agent-made deep tree cannot exhaust fds
#: or the stack (the store removes only its own shallow pending directories)
MAX_RM_DEPTH = 8

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC
_NOCTTY = getattr(os, "O_NOCTTY", 0)

Rel = str | os.PathLike[str]


def rm_at(dir_fd: int, name: str, depth: int = 0) -> None:
    """Remove ``name`` below ``dir_fd``: a directory by its children first (each opened ``O_NOFOLLOW``), anything
    else (a symlink too) unlinked, never followed. A tree deeper than :data:`MAX_RM_DEPTH` refuses (``OSError``)."""
    if "/" in name or name in ("", ".", "..") or "\0" in name:
        raise OSError(errno.EINVAL, "not a single name", name)
    try:
        fd = os.open(name, _DIR | _NOFOLLOW | _NOCTTY, dir_fd=dir_fd)
    except OSError as e:
        if isinstance(e, FileNotFoundError):
            raise
        os.unlink(name, dir_fd=dir_fd)  # a file or a symlink (ELOOP / ENOTDIR on the directory open)
        return
    try:
        for child in os.listdir(fd):
            if depth >= MAX_RM_DEPTH:
                raise OSError(errno.ELOOP, f"a tree deeper than {MAX_RM_DEPTH} levels is not removed", name)
            rm_at(fd, child, depth + 1)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dir_fd)


def remove_made_dir(project: str | os.PathLike[str], name: str, recorded: Sequence[int] | None) -> bool:
    """Remove the directory ``project/name`` only if it is the one recorded (``[st_dev, st_ino]``: the directory a
    call itself made), by fds: ``name`` is opened ``O_NOFOLLOW`` below the project fd and compared, renamed to a unique
    tombstone, the tombstone checked to be that same directory again, its entries removed through the verified fd and
    the (then empty) tombstone dropped with ``rmdir`` below the project fd. Anything else (another inode, a link, no
    record) is a person's own and is left alone. ``True`` when the directory was removed."""
    if recorded is None or "/" in name or name in ("", ".", ".."):
        return False
    flags = _DIR | _NOFOLLOW
    try:
        pfd = os.open(project, flags)
    except OSError:
        return False
    try:
        try:
            dfd = os.open(name, flags, dir_fd=pfd)
        except OSError:
            return False
        try:
            st = os.fstat(dfd)
            if [st.st_dev, st.st_ino] != list(recorded):
                return False
            tomb = f".{name.lstrip('.')}-rollback-{secrets.token_hex(8)}"
            try:
                os.rename(name, tomb, src_dir_fd=pfd, dst_dir_fd=pfd)
            except OSError:
                return False
            try:
                moved = os.stat(tomb, dir_fd=pfd, follow_symlinks=False)
            except OSError:
                return False
            if (moved.st_dev, moved.st_ino) != (st.st_dev, st.st_ino):
                with contextlib.suppress(OSError):  # not what we opened: put it back, touch nothing
                    os.rename(tomb, name, src_dir_fd=pfd, dst_dir_fd=pfd)
                return False
            try:
                for child in os.listdir(dfd):
                    rm_at(dfd, child)
                os.rmdir(tomb, dir_fd=pfd)  # only an empty directory can go
            except OSError:
                return False
            return True
        finally:
            os.close(dfd)
    finally:
        os.close(pfd)


def _close(fd: list[int], cache: dict[str, int]) -> None:
    while cache:
        with contextlib.suppress(OSError):
            os.close(cache.popitem()[1])
    if fd and fd[0] >= 0:
        with contextlib.suppress(OSError):
            os.close(fd[0])
        fd[0] = -1


class RootFS:
    """The workspace root, held open. Build it with :meth:`at`; :meth:`close` releases the one fd."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._parts = path.parts
        self._str = os.fspath(path)
        self._prefix = self._str.rstrip("/") + "/"
        self._fd = [fd]
        self._cache: dict[str, int] = {}
        self._finalizer = weakref.finalize(self, _close, self._fd, self._cache)
        st = os.fstat(fd)
        self.ident: tuple[int, int] = (st.st_dev, st.st_ino)
        #: set by ``open_store_fs``: the layout resolve that opened this root found an orch ``config.json``
        self.config_seen = False

    @classmethod
    def at(cls, path: Rel, *, ident: tuple[int, int] | None = None, create: bool = False) -> RootFS:
        """Open the root ``path`` (``O_NOFOLLOW`` on its last component). ``ident``: the ``(st_dev, st_ino)``
        discovery saw; another directory there refuses (``validation.path``). ``create`` (a new workspace only, never
        with ``ident``): make the directory first."""
        p = Path(path)
        if create and ident is None:
            p.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(p, _DIR | _NOFOLLOW)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR) and os.path.islink(p):
                raise StoreError("validation.path", f"the workspace root {p} is a symlink") from None
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):  # pragma: no cover - O_DIRECTORY refuses
                raise StoreError("validation.path", f"the workspace root {p} is not a directory")
            if ident is not None and (st.st_dev, st.st_ino) != tuple(ident):
                raise StoreError(
                    "validation.path", f"{p} is no longer the workspace directory that was found; nothing was opened"
                )
            return cls(p, fd)
        except BaseException:
            os.close(fd)
            raise

    @property
    def fd(self) -> int:
        fd = self._fd[0]
        if fd < 0:
            raise StoreError("validation.path", "the workspace root is closed")
        return fd

    def close(self) -> None:
        self._finalizer()

    def __enter__(self) -> RootFS:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the one path interpretation
    def rel(self, p: Rel) -> tuple[str, ...]:
        """The components of ``p`` below the root, lexically (no file system access): a ``str`` not starting with
        ``/`` is relative to the root (``""`` is the root), anything else must start with the root's components."""
        s = os.fspath(p)
        if "\0" in s or "\\" in s:
            raise StoreError("validation.path", f"{s!r} escapes the workspace")
        if isinstance(p, str) and not s.startswith("/"):
            parts = tuple(s.split("/")) if s else ()
        elif s.startswith(self._prefix):  # the store's own paths (root / ...): a plain string split, it is hot
            rest = s[len(self._prefix) :]
            parts = tuple(rest.split("/")) if rest else ()
        elif s == self._str:
            parts = ()
        else:
            whole = Path(s).parts
            n = len(self._parts)
            if whole[:n] != self._parts:
                raise StoreError("validation.path", f"{s!r} is outside the workspace")
            parts = whole[n:]
        if any(c in ("", ".", "..") for c in parts):
            raise StoreError("validation.path", f"{s!r} escapes the workspace")
        if len(parts) > MAX_DEPTH:
            raise StoreError("validation.path", f"{s!r} is deeper than {MAX_DEPTH} components")
        return parts

    def _refuse(self, parts: tuple[str, ...], i: int, dfd: int, e: OSError) -> BaseException:
        """What a failed open of directory component ``parts[i]`` (below ``dfd``) means: a symlink refuses
        (``validation.path``), anything else is the ``OSError`` itself."""
        if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            try:
                st = os.stat(parts[i], dir_fd=dfd, follow_symlinks=False)
            except OSError:
                return e
            if stat.S_ISLNK(st.st_mode):
                rel = "/".join(parts)
                return StoreError("validation.path", f"{rel!r} passes through a symlink ({'/'.join(parts[: i + 1])})")
        return e

    @contextlib.contextmanager
    def _dir(self, parts: tuple[str, ...], *, create: bool = False, mode: int = 0o777) -> Iterator[int]:
        """A directory fd for ``parts`` (the root's own fd for none), walked ``O_NOFOLLOW``; closed on exit.
        ``create``: make missing directories on the way."""
        cur = self.fd
        opened: list[int] = []
        start = 0
        if parts and parts[0] in CACHED:
            cached = self._cache.get(parts[0])
            if cached is None:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(parts[0], mode, dir_fd=cur)
                try:
                    cached = os.open(parts[0], _DIR | _NOFOLLOW, dir_fd=cur)
                except OSError as e:
                    raise self._refuse(parts, 0, cur, e) from None
                self._cache[parts[0]] = cached
            cur, start = cached, 1
        try:
            for i, comp in enumerate(parts):
                if i < start:
                    continue
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(comp, mode, dir_fd=cur)
                try:
                    nxt = os.open(comp, _DIR | _NOFOLLOW, dir_fd=cur)
                except OSError as e:
                    raise self._refuse(parts, i, cur, e) from None
                opened.append(nxt)
                cur = nxt
            yield cur
        finally:
            for fd in opened:
                os.close(fd)

    def _forget(self, p: Rel) -> None:
        """``p`` is about to be removed or renamed: if it is a cached top-level directory, drop its fd first."""
        parts = self.rel(p)
        if len(parts) == 1 and parts[0] in self._cache:
            with contextlib.suppress(OSError):
                os.close(self._cache.pop(parts[0]))

    def drop_cache(self) -> None:
        """Close the cached top-level directory fds (the next access walks again)."""
        while self._cache:
            with contextlib.suppress(OSError):
                os.close(self._cache.popitem()[1])

    def revalidate(self) -> None:
        """Drop every cached top-level directory fd whose name below the root is no longer that directory (an
        ``lstat`` relative to the root fd: a symlink, another directory, or nothing there). Cheap: one ``lstat`` and
        one ``fstat`` per cached fd."""
        for name, fd in list(self._cache.items()):
            try:
                st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                cur = os.fstat(fd)
                same = stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (cur.st_dev, cur.st_ino)
            except OSError:
                same = False
            if not same:
                del self._cache[name]
                with contextlib.suppress(OSError):
                    os.close(fd)

    @contextlib.contextmanager
    def _parent(self, p: Rel, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = self.rel(p)
        if not parts:
            raise StoreError("validation.path", "the workspace root itself is not a file")
        with self._dir(parts[:-1], create=create) as dfd:
            yield dfd, parts[-1]

    @contextlib.contextmanager
    def dir(self, p: Rel = "") -> Iterator[int]:
        """The directory ``p`` (the root for ``""``) as an fd, for a loop that works below it; closed on exit."""
        with self._dir(self.rel(p)) as fd:
            yield fd

    # -- opening and reading
    def open(self, p: Rel, flags: int, mode: int = 0o644, *, parents: bool = False) -> int:
        """``os.open`` of ``p`` below the root: every directory component ``O_NOFOLLOW``, the last one too."""
        with self._parent(p, create=parents) as (dfd, name):
            if flags & getattr(os, "O_DIRECTORY", 0):
                return os.open(name, flags | _NOFOLLOW | _CLOEXEC, mode, dir_fd=dfd)
            return fsio.open_regular(name, flags, mode, dir_fd=dfd)  # a FIFO never blocks: regular files only

    def read_or_none(self, p: Rel, limit: int | None = MAX_READ) -> bytes | None:
        """The bytes of ``p``; ``None`` if it is missing or a symlink (never followed). At most ``limit`` + 1 bytes
        are read (``fsio.read_or_none``'s contract). A symlinked directory on the way refuses (``validation.path``)."""
        try:
            fd = self.open(p, os.O_RDONLY)  # fsio.open_regular: O_NONBLOCK, a regular file only, then blocking
        except (FileNotFoundError, NotADirectoryError, fsio.NotRegularFile):
            return None  # a FIFO or a device counts as absent: never read (it could block or never end)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK):
                return None
            raise
        with os.fdopen(fd, "rb") as f:
            return f.read() if limit is None else f.read(limit + 1)

    def tail_bytes(self, p: Rel, n: int) -> tuple[int, bytes]:
        """``(size, last n bytes)`` of a file (a symlink or a missing file is ``(0, b"")``)."""
        try:
            fd = self.open(p, os.O_RDONLY)
        except OSError:
            return 0, b""
        with os.fdopen(fd, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - n))
            return size, f.read(n)

    # -- metadata and listings
    def lstat(self, p: Rel) -> os.stat_result:
        """``lstat`` of ``p`` (its directories walked ``O_NOFOLLOW``); the root's own ``fstat`` for ``""``."""
        parts = self.rel(p)
        if not parts:
            return os.fstat(self.fd)
        with self._dir(parts[:-1]) as dfd:
            return os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)

    def leaf_stats(self, p: Rel, names: Iterable[str], leaf: str) -> dict[str, os.stat_result]:
        """``lstat`` of ``<p>/<name>/<leaf>`` for each ``name``, **metadata only** (the store's per-ticket log
        signatures, its hot path): ``p`` is opened once (``O_NOFOLLOW`` walk) and each ``name`` must be a real
        directory by ``lstat`` below it; then ``name/leaf`` is ``lstat``-ed relative to that one fd, without opening
        the ticket directory (an open per ticket per lock costs more than the whole append). A missing entry, or a
        ``name`` that is a symlink, is left out. The one place a component below the root is resolved by the kernel
        rather than walked: only in a race (the directory swapped between the two ``lstat``) and only for a
        signature, never for bytes; every read and write still walks ``O_NOFOLLOW`` (#516)."""
        out: dict[str, os.stat_result] = {}
        try:
            cm = self.dir(p)
            pfd = cm.__enter__()
        except (FileNotFoundError, NotADirectoryError):
            return out
        try:
            for name in names:
                if not name or "/" in name or name in (".", ".."):
                    continue
                try:
                    if not stat.S_ISDIR(os.stat(name, dir_fd=pfd, follow_symlinks=False).st_mode):
                        continue
                    out[name] = os.stat(f"{name}/{leaf}", dir_fd=pfd, follow_symlinks=False)
                except OSError:
                    continue
        finally:
            cm.__exit__(None, None, None)
        return out

    def _mode(self, p: Rel) -> int | None:
        try:
            return self.lstat(p).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        except StoreError:  # a symlinked directory on the way: nothing there is the workspace's
            return None

    def lexists(self, p: Rel) -> bool:
        return self._mode(p) is not None

    def is_dir(self, p: Rel) -> bool:
        """A real directory (a symlink to one is not)."""
        m = self._mode(p)
        return m is not None and stat.S_ISDIR(m)

    def is_file(self, p: Rel) -> bool:
        """A regular file (a symlink to one is not)."""
        m = self._mode(p)
        return m is not None and stat.S_ISREG(m)

    def is_link(self, p: Rel) -> bool:
        m = self._mode(p)
        return m is not None and stat.S_ISLNK(m)

    def listdir(self, p: Rel = "") -> list[str]:
        """The names in the directory ``p`` (opened ``O_NOFOLLOW``: a symlinked directory refuses)."""
        with self.dir(p) as fd:
            return os.listdir(fd)

    def listdir_or_empty(self, p: Rel) -> list[str]:
        """:meth:`listdir`, ``[]`` when ``p`` is missing, not a directory or a symlink."""
        try:
            return self.listdir(p)
        except (FileNotFoundError, NotADirectoryError):
            return []
        except StoreError:
            return []
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK):
                return []
            raise

    def regular_files(self, p: Rel) -> list[str]:
        """The names of the regular files in the directory ``p`` (one fd for the listing and every ``lstat``: a
        symlink, a directory or anything else is left out); ``[]`` when ``p`` is missing or a symlink."""
        try:
            cm = self.dir(p)
            fd = cm.__enter__()
        except (FileNotFoundError, NotADirectoryError, StoreError):
            return []
        try:
            out = []
            for n in os.listdir(fd):
                with contextlib.suppress(OSError):
                    if stat.S_ISREG(os.stat(n, dir_fd=fd, follow_symlinks=False).st_mode):
                        out.append(n)
            return out
        finally:
            cm.__exit__(None, None, None)

    # -- writing
    def mkdir(self, p: Rel, *, mode: int = 0o777) -> None:
        """Make ``p`` and its missing parents (each one ``O_NOFOLLOW``: a symlink on the way refuses)."""
        with self._dir(self.rel(p), create=True, mode=mode):
            pass

    def fsync_dir(self, p: Rel = "") -> None:
        with self.dir(p) as fd:
            os.fsync(fd)

    def write_atomic(self, p: Rel, data: bytes, *, mode: int = 0o644, durable: bool = True) -> None:
        """Replace ``p`` with ``data`` (temp file, fsync, rename, directory fsync): a reader sees the old file or the
        new one. A symlink at ``p`` is replaced, never written through."""
        with self._parent(p, create=True) as (dfd, name):
            tmp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, mode, dir_fd=dfd)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    if durable:
                        os.fsync(f.fileno())
                os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=dfd)
                raise
            if durable:
                os.fsync(dfd)

    def append_durable(self, p: Rel, data: bytes, *, commit: bool = False) -> None:
        """Append ``data`` to ``p`` (created if missing, never through a symlink) and fsync it (and the directory when
        it is new). ``commit``: the log append, ``F_FULLFSYNC`` on macOS."""
        with self._parent(p, create=True) as (dfd, name):
            try:
                os.stat(name, dir_fd=dfd, follow_symlinks=False)
                new = False
            except FileNotFoundError:
                new = True
            fd = fsio.open_regular(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644, dir_fd=dfd)
            try:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                if commit:
                    fsio._full_fsync(fd)  # the one F_FULLFSYNC helper (looked up here: tests replace it)
                else:
                    os.fsync(fd)
            finally:
                os.close(fd)
            if new:
                os.fsync(dfd)

    def replace(self, src: Rel, dst: Rel) -> None:
        """``os.replace`` of ``src`` onto ``dst``, both below the root, by their parent fds (``dst``'s parents are
        made). A symlink at ``dst`` is replaced, never followed."""
        self._forget(src)
        self._forget(dst)
        with self._parent(src) as (sfd, sname), self._parent(dst, create=True) as (dfd, dname):
            os.replace(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)

    def unlink(self, p: Rel, *, missing_ok: bool = False) -> None:
        self._forget(p)
        try:
            with self._parent(p) as (dfd, name):
                os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmdir(self, p: Rel) -> None:
        self._forget(p)
        with self._parent(p) as (dfd, name):
            os.rmdir(name, dir_fd=dfd)

    def rmtree(self, p: Rel, *, ignore_errors: bool = False) -> None:
        """Remove a tree below the root, walked ``O_NOFOLLOW`` (a symlink is unlinked, never followed)."""
        try:
            self._forget(p)
            with self._parent(p) as (dfd, name):
                rm_at(dfd, name)
        except (OSError, StoreError):
            if not ignore_errors:
                raise
