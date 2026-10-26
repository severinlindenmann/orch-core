"""The capabilities a handler gets (D-P2-9): environment, files, exec, git, signer, clock.

Handlers never touch ``os.environ``, ``open()`` or ``subprocess`` themselves; they ask ``ctx.caps``. This module is the
**only** place under ``orch.ops`` that does (``tests/ops/test_capability_boundary.py`` pins that).

| Capability | Host (:meth:`Capabilities.host`)        | Direct mode (:meth:`Capabilities.direct`) |
|------------|-----------------------------------------|-------------------------------------------|
| ``env``    | fixed and minimal, never the client's   | the CLI's environment                     |
| ``files``  | only under the roots; no cwd, no stdin  | any path the CLI names; cwd; stdin        |
| ``exec``   | none (raises :class:`CapabilityDenied`) | the subprocess runner (``verify.cmd``)    |
| ``git``    | reader children under pins (H3b1)       | the same reader in-process, no git        |
|            |                                         | process (``store.observe``, #565)         |
| ``signer`` | none                                    | custody                                   |
| ``clock``  | host clock                              | system clock                              |

Reading D-P2-9 strictly (an ambiguity, the stricter side taken): in host mode ``files`` is limited to the roots even
for reads of client-named paths (the client sends bytes through ``ctx.files`` instead), the working directory and
stdin are denied, and ``exec`` is denied outright rather than "restricted". ``git`` in host mode is only the
reader launcher (:mod:`orch.gitread.client`); a host built without one denies git.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
import weakref
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch.layout import Layout

WHOLE_FILE_LIMIT = 64 << 20  # host mode: read_bytes and read_text refuse a larger file

__all__ = [
    "CapabilityDenied",
    "Capabilities",
    "Exec",
    "Files",
    "Git",
    "NotARegularFile",
    "Proc",
    "Signer",
    "open_tty",
]

Proc = subprocess.Popen  # the type of a spawned process, for annotations elsewhere in orch.ops


class CapabilityDenied(PermissionError):
    """The handler asked for a power its context does not have (host mode). An ``OSError``, so the call sites that
    already treat "cannot run" or "cannot read" as a refusal keep doing so."""

    def __init__(self, what: str, message: str | None = None) -> None:
        super().__init__(message or f"{what}: not available to a handler in this mode")
        self.what = what


def open_tty(flags: int) -> int:
    """Open the person's own terminal (``/dev/tty``) as a file descriptor. The terminal is the custody prompt's
    channel, not a file capability; in host mode the audit hook refuses it (outside every root)."""
    return os.open("/dev/tty", flags | os.O_NOCTTY)


class Files:
    """Filesystem access. Build it with :meth:`direct` or :meth:`host`; the bare constructor has no defaults.

    Direct mode (``roots`` ``None``): any path, the plain ``os``/``pathlib`` calls, as before.

    Host mode: **one interpretation of a path, and the check is the open.** The root directories are opened once, as
    directory file descriptors, when the capability is built. A path must be absolute and start with the same
    components as a root (a plain comparison of components: no ``realpath``, no ``normpath``, no case folding). The
    rest is walked component by component with ``dir_fd`` and ``O_NOFOLLOW``, so no symlink is followed, and ``..``,
    ``.``, empty components, NUL bytes and relative paths are refused. There is no separate "validate, then open" step
    for a swap to land in. The opened object must be a regular file (``O_NONBLOCK`` and ``O_NOCTTY``, then ``fstat``)
    with one link; a write truncates only after that check. Stricter than the D-P2-9 text: a path that reaches a
    root only through a symlink (``/var`` for ``/private/var``) is refused; the host names its roots by their real
    paths. The root directories themselves are trusted: the host owns them.

    **Client-named paths** (``client_paths``): ``False`` in host mode. ``--file`` and artifact arguments then take
    their bytes only from ``ctx.files`` (what the client uploaded, section 0.10); a path is refused.

    **Lifetime.** Host ``Files`` holds one fd per root. Build it **once per workspace** and keep it; ``close()`` (or
    ``with``) releases the fds, and a ``weakref.finalize`` does so if it is dropped.
    """

    def __init__(
        self,
        *,
        roots: tuple[Path, ...] | None,
        cwd_allowed: bool,
        stdin_allowed: bool,
        client_paths: bool,
    ) -> None:
        self.cwd_allowed = cwd_allowed
        self.stdin_allowed = stdin_allowed
        self.client_paths = client_paths
        self.roots = roots
        self._root_fds: list[tuple[tuple[str, ...], int]] = []
        self._finalizer = weakref.finalize(self, _close_all, self._root_fds)
        for r in roots or ():
            rp = os.fspath(r)
            if "\0" in rp or not os.path.isabs(rp):
                self.close()
                raise ValueError(f"bad root: {rp!r}")
            parts = tuple(c for c in rp.split("/") if c)
            try:
                fd = os.open(rp, os.O_RDONLY | os.O_DIRECTORY)
            except OSError:
                self.close()
                raise
            self._root_fds.append((parts, fd))

    @classmethod
    def direct(cls) -> Files:
        """Today's CLI: any path, the cwd, stdin, client-named files."""
        return cls(roots=None, cwd_allowed=True, stdin_allowed=True, client_paths=True)

    @classmethod
    def host(cls, roots: tuple[str | os.PathLike[str], ...]) -> Files:
        return cls(roots=tuple(Path(r) for r in roots), cwd_allowed=False, stdin_allowed=False, client_paths=False)

    def close(self) -> None:
        self._finalizer()

    def __enter__(self) -> Files:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the one path interpretation (host mode)
    def _split(self, path: str | os.PathLike[str]) -> tuple[int, tuple[str, ...]]:
        """The root fd and the components below the root. Pure string work, no filesystem access."""
        p = os.fspath(path)
        if isinstance(p, bytes):  # pragma: no cover
            raise CapabilityDenied("bytes path")
        if "\0" in p:
            raise CapabilityDenied("NUL in path")
        if not p.startswith("/"):
            raise CapabilityDenied(f"relative path: {p!r}")
        parts = p.split("/")[1:]
        if parts and parts[-1] == "":  # one trailing slash is the directory itself
            parts = parts[:-1]
        if any(c in ("", ".", "..") for c in parts):
            raise CapabilityDenied(f"path with an empty, '.' or '..' component: {p!r}")
        for root_parts, fd in self._root_fds:
            n = len(root_parts)
            if tuple(parts[:n]) == root_parts:
                return fd, tuple(parts[n:])
        raise CapabilityDenied(f"path outside the roots: {p!r}")

    @contextlib.contextmanager
    def _parent(self, path: str | os.PathLike[str], *, create: bool = False) -> Iterator[tuple[int, str | None]]:
        """A directory fd for the parent of ``path`` (walked with ``O_NOFOLLOW``) and the final name (``None`` when
        ``path`` is a root). ``create``: make missing intermediate directories."""
        root_fd, rest = self._split(path)
        opened: list[int] = []
        try:
            cur = root_fd
            for comp in rest[:-1]:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(comp, 0o777, dir_fd=cur)
                cur = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
                opened.append(cur)
            yield cur, (rest[-1] if rest else None)
        finally:
            for fd in opened:
                os.close(fd)

    def _open(self, path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if self.roots is None:
            return os.open(os.fspath(path), flags, mode)
        trunc = flags & os.O_TRUNC
        with self._parent(path) as (dfd, name):
            if name is None:  # a root itself: a directory, by its (trusted) fd
                if not flags & os.O_DIRECTORY:
                    raise NotARegularFile(os.fspath(path))
                return os.dup(dfd)
            # O_NONBLOCK: a FIFO cannot hang the open. O_NOCTTY: a terminal device cannot become ours. No O_TRUNC
            # yet: it would destroy a file this check is about to refuse.
            fd = os.open(name, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, mode, dir_fd=dfd)
        try:
            st = os.fstat(fd)
            if flags & os.O_DIRECTORY:
                if not stat.S_ISDIR(st.st_mode):
                    raise NotARegularFile(os.fspath(path))
            else:
                if not stat.S_ISREG(st.st_mode):
                    raise NotARegularFile(os.fspath(path))
                if st.st_nlink > 1:
                    raise CapabilityDenied(f"hard-linked file: {os.fspath(path)!r}")
            os.set_blocking(fd, True)
            if trunc:
                os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise
        return fd

    # -- the capability
    def getcwd(self) -> str:
        if not self.cwd_allowed:
            raise CapabilityDenied("working directory")
        return os.getcwd()

    def home(self) -> str:
        """The user's home directory by the system's own rules (``HOME``, else the password database)."""
        if not self.cwd_allowed:
            raise CapabilityDenied("home directory")
        return os.path.expanduser("~")

    def read_stdin(self, limit: int) -> bytes:
        """At most ``limit`` bytes of standard input."""
        if not self.stdin_allowed:
            raise CapabilityDenied("stdin")
        stream = getattr(sys.stdin, "buffer", None)
        return stream.read(limit) if stream is not None else sys.stdin.read(limit).encode()

    def _open_client(self, path: str) -> tuple[int, os.stat_result]:
        """A client-named regular file, opened ``O_NONBLOCK`` (a FIFO cannot hang the call)."""
        if not self.client_paths:
            raise CapabilityDenied("a path named by the client: send the bytes instead")
        fd = self._open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))  # never a link
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise NotARegularFile(path)
        return fd, st

    def read_client(self, path: str, limit: int) -> tuple[bytes, int]:
        """Up to ``limit`` bytes of the client-named regular file ``path`` and the file's size; a file larger than
        ``limit`` is not read (``(b"", size)``). :class:`CapabilityDenied` where client paths are off."""
        fd, st = self._open_client(path)
        with os.fdopen(fd, "rb") as f:
            if st.st_size > limit:
                return b"", st.st_size
            return f.read(limit), st.st_size

    def sha256_client(self, path: str, limit: int) -> str | None:
        """The SHA-256 of the client-named file, read in 1 MiB chunks; ``None`` if it is larger than ``limit``."""
        fd, st = self._open_client(path)
        h, total = hashlib.sha256(), 0
        with os.fdopen(fd, "rb") as f:
            if st.st_size > limit:
                return None
            while chunk := f.read(1 << 20):
                total += len(chunk)
                if total > limit:
                    return None
                h.update(chunk)
        return h.hexdigest()

    def open_fd(self, path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        return self._open(path, flags, mode)

    def read_bytes(self, path: str | os.PathLike[str]) -> bytes:
        if self.roots is None:
            return Path(path).read_bytes()
        with os.fdopen(self._open(path, os.O_RDONLY), "rb") as f:
            data = f.read(WHOLE_FILE_LIMIT + 1)
        if len(data) > WHOLE_FILE_LIMIT:
            raise CapabilityDenied(f"file larger than {WHOLE_FILE_LIMIT} bytes: {os.fspath(path)!r}")
        return data

    def read_text(self, path: str | os.PathLike[str]) -> str:
        return self.read_bytes(path).decode("utf-8")

    def write_text(self, path: str | os.PathLike[str], text: str) -> None:
        if self.roots is None:
            Path(path).write_text(text, encoding="utf-8")
            return
        with os.fdopen(self._open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666), "wb") as f:
            f.write(text.encode("utf-8"))

    def mkdir(self, path: str | os.PathLike[str], *, parents: bool = False, mode: int = 0o777) -> None:
        if self.roots is None:
            Path(path).mkdir(parents=parents, mode=mode, exist_ok=True)
            return
        with self._parent(path, create=parents) as (dfd, name):
            if name is not None:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, mode, dir_fd=dfd)

    def unlink(self, path: str | os.PathLike[str]) -> None:
        if self.roots is None:
            os.unlink(path)
            return
        with self._parent(path) as (dfd, name):
            if name is None:
                raise CapabilityDenied("a root cannot be removed")
            os.unlink(name, dir_fd=dfd)

    def rmtree(self, path: str | os.PathLike[str], *, ignore_errors: bool = False) -> None:
        """Remove a directory tree. Host mode walks it with ``dir_fd`` and ``O_NOFOLLOW`` (symlinks are unlinked,
        never followed) and **raises on the first failure** unless ``ignore_errors``."""
        if self.roots is None:
            shutil.rmtree(path, ignore_errors=ignore_errors)
            return
        try:
            with self._parent(path) as (dfd, name):
                if name is None:
                    raise CapabilityDenied("a root cannot be removed")
                _rm_at(dfd, name)
        except OSError:
            if not ignore_errors:
                raise

    def listdir(self, path: str | os.PathLike[str]) -> list[str]:
        """The names in a directory (host mode: opened by the fd walk, never through a link)."""
        if self.roots is None:
            return os.listdir(path)
        fd = self._open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)

    def lexists(self, path: str | os.PathLike[str]) -> bool:
        if self.roots is None:
            return os.path.lexists(path)
        try:
            with self._parent(path) as (dfd, name):
                os.lstat(name if name is not None else ".", dir_fd=dfd)
        except OSError:
            return False
        return True


def _close_all(fds: list[tuple[tuple[str, ...], int]]) -> None:
    while fds:
        _, fd = fds.pop()
        with contextlib.suppress(OSError):
            os.close(fd)


def _rm_at(dir_fd: int, name: str) -> None:
    """Remove ``name`` below ``dir_fd``: a directory by its children first, anything else (a symlink too) unlinked."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOCTTY, dir_fd=dir_fd)
    except (NotADirectoryError, OSError) as e:
        if isinstance(e, FileNotFoundError):
            raise
        os.unlink(name, dir_fd=dir_fd)  # a file or a symlink (ELOOP / ENOTDIR on the directory open)
        return
    try:
        for child in os.listdir(fd):
            _rm_at(fd, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dir_fd)


class NotARegularFile(CapabilityDenied):
    """A directory, FIFO, socket or device where a regular file was expected."""

    def __init__(self, path: str) -> None:
        super().__init__("file", f"{path!r} is not a regular file")


@dataclass(frozen=True)
class Exec:
    """Process spawning (``verify.cmd``). ``enabled`` ``False``: every call raises :class:`CapabilityDenied`.

    ``base_env`` is the environment a spawned command starts from, before the caller's allow-list: the process
    environment in direct mode."""

    enabled: bool
    base_env: Callable[[], Mapping[str, str]] = field(repr=False)

    def popen(self, argv: list[str], **kw: Any) -> Proc[bytes]:
        if not self.enabled:
            raise CapabilityDenied("exec")
        return subprocess.Popen(argv, **kw)

    def run(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        if not self.enabled:
            raise CapabilityDenied("exec")
        return subprocess.run(argv, **kw)

    def killpg(self, pgid: int, sig: int) -> None:
        """Signal a process group this capability started."""
        if not self.enabled:
            raise CapabilityDenied("exec")
        os.killpg(pgid, sig)

    def child_env(self, extra: Mapping[str, str]) -> dict[str, str]:
        if not self.enabled:
            raise CapabilityDenied("exec")
        return {**self.base_env(), **extra}


@dataclass(frozen=True)
class WorkingCopy:
    """The working copy of one (ticket, repo) for D58 and ``tree.check`` (H11d, :meth:`Git.ticket_copy`)."""

    path: Path | None
    #: the registration generation of (ticket, repo): every create and every removal bumps it
    gen: int
    #: the registered worktree's pin ``{path, dev, ino}``, ``None`` for the declared working copy
    pin: dict[str, Any] | None
    registered: bool
    #: why there is no path (an unpinned registration, an undeclared repo), else ``None``
    why: str | None

    def binding(self, uid: str, name: str) -> dict[str, Any]:
        """What a prepared request records for this repo (the binding of §12.7)."""
        return {"repo": name, "ticket": uid, "gen": self.gen, "registered": self.registered, "pin": self.pin}


@dataclass(frozen=True)
class Git:
    """Reading the linked repositories.

    * **Direct mode** (``reader`` ``None``): orch's own reader in-process (:data:`orch.gitread.local.READER`, through
      ``orch.store.observe``; :meth:`diff` through :mod:`orch.gitread.local`). No git process (#565): nothing the
      repository's config says can run, and there is no gate-then-spawn window to race. What the reader cannot read
      is ``None`` ("cannot be told"), never clean.
    * **Host mode** (``reader`` a :class:`orch.gitread.client.HostGitReader`): no git process; every read is a
      ``python -I -m orch.gitread`` child under the repo's pins (D-P2-19). :meth:`dirty` is the ``tree.check`` job
      (H10a, :mod:`orch.gitread.treejob`): orch's own index and worktree reader in a ``worktree``-profile child, never
      git (F2). It answers ``None`` ("cannot be asked") whenever no clean answer can be established, so ``verdict``
      and ``approve code`` fail closed (``observe.unavailable``). :meth:`run` (an arbitrary git command) is denied.
    * **Principal** (host mode): every read runs under the caller's principal, so the pool's one-child-per-principal
      rule (F10) is real. The dispatcher binds it per request with :meth:`Capabilities.with_principal`; a host-mode
      read without one is refused.
    """

    enabled: bool
    reader: Any = field(default=None, repr=False)
    principal: str | None = None
    #: direct mode (H11d, D-P2-38): the worktree registrations (a :class:`orch.gitread.client.PinStore` read from the
    #: host's pin file of this workspace), or ``None``: no registration. Host mode reads ``reader.pins``.
    worktrees: Any = field(default=None, repr=False)

    def _need(self) -> None:
        if not self.enabled:
            raise CapabilityDenied("git")
        if self.reader is not None and not self.principal:
            raise CapabilityDenied("git", "git: a host-mode read needs the caller's principal")

    def repo_path(self, layout: Layout, repos: Any, name: str) -> Path | None:
        """Where repo ``name`` of ``settings.repos`` lives. Direct mode: P1's ``observe.repo_path`` (resolved).
        Host mode: joined and normalised **lexically**, never resolved: the components are agent-writable, and the
        reader's pins are keyed by this path (a symlink planted there must not lead to another repo's pins)."""
        from orch.store import observe

        if self.reader is None:
            return observe.repo_path(layout, repos, name)
        spec = repos.get(name)
        if not spec:
            return None
        return Path(os.path.normpath(os.path.join(os.fspath(layout.project), spec["path"])))

    def ticket_copy(self, layout: Layout, repos: Any, uid: str, name: str) -> WorkingCopy:
        """H11d (D-P2-32, §12.7): the working copy of ticket ``uid`` for repo ``name`` that D58 and ``tree.check``
        read. A registered worktree is that ticket's working copy, and **only** it: an unpinned registration has no
        path (``why`` says so) and never falls back to the declared working copy. Without a registration it is the
        declared working copy of ``settings.repos``. ``gen`` and ``pin`` are what a prepared request binds."""
        registry = self.reader.pins if self.reader is not None else self.worktrees
        slot = registry.worktree(uid, name) if registry is not None else {"gen": 0, "reg": None}
        reg = slot["reg"]
        if reg is None:
            return WorkingCopy(self.repo_path(layout, repos, name), slot["gen"], None, False, None)
        if not repos.get(name):
            return WorkingCopy(None, slot["gen"], None, True, f"{name}: the repository is no longer declared")
        if reg["pins"] is None:
            return WorkingCopy(None, slot["gen"], None, True, f"{name}: the ticket's worktree is not pinned")
        return WorkingCopy(Path(reg["path"]), slot["gen"], reg["pins"]["worktree"], True, None)

    def _look(self, path: Path, refs: list[str]) -> dict[str, Any] | None:
        from orch.gitread.errors import Unavailable

        try:
            return self.reader.observe(path, refs, principal=self.principal)
        except Unavailable:
            return None

    def head(self, path: Path) -> str | None:
        self._need()
        if self.reader is not None:
            got = self._look(path, [])
            return got["head_sha"] if got else None
        from orch.store import observe

        return observe.head(path)

    def symbolic_head(self, path: Path) -> str | None:
        """The branch ref ``HEAD`` names (``refs/heads/<branch>``), ``None`` when detached or unreadable."""
        self._need()
        if self.reader is not None:
            got = self._look(path, [])
            return got["head_ref"] if got else None
        from orch.store import observe

        return observe.symbolic_head(path)

    def dirty(self, path: Path, name: str | None = None) -> str | None:
        """What is uncommitted in the working copy at ``path``: ``""`` when clean, lines when not, ``None`` when it
        cannot be told. Host mode: the ``tree.check`` job for repo ``name`` (``why: path`` lines, every path
        escaped); unknown (no pin, a refusal, a limit, tier B without its worker) is ``None``."""
        self._need()
        if self.reader is not None:
            return self.tree_lines(path, name)
        from orch.store import observe

        return observe.dirty(path)

    def tree_check(self, path: Path, name: str | None = None, *, removal: bool = False) -> dict[str, Any] | None:
        """Host mode only: the ``TreeCheckResult`` of ``path``, or ``None`` when no clean answer can be established
        (fail closed). Never a git process (F2)."""
        self._need()
        if self.reader is None:
            raise CapabilityDenied("git", "git: tree.check is the host's reader job")
        from orch.gitread.errors import Unavailable

        label = name if name is not None else path.name  # never cut: job_input refuses over 64 (unknown)
        try:
            if removal:
                return self.reader.tree_check(path, label, principal=self.principal, removal=True)
            return self.reader.tree_check(path, label, principal=self.principal)
        except Unavailable:
            return None

    def tree_lines(self, path: Path, name: str | None = None) -> str | None:
        from orch.gitread.treejob import render

        got = self.tree_check(path, name)
        return None if got is None else render(got)

    def identity(self, path: Path, name: str) -> str:
        self._need()
        if self.reader is not None:
            from orch.gitread.client import identity_of

            got = self._look(path, [])
            return identity_of(got["origin_urls"], name) if got else f"local:{name}"
        from orch.store import observe

        return observe.repo_identity(path, name)

    def observe(self, store: Any, ref: str, problems: list[str] | None = None) -> list[dict[str, Any]]:
        """Append what git owes the ticket. Where git is off, nothing is observed and that is a *problem line* (the
        callers show it, or refuse with ``observe.unavailable``), not an internal error."""
        if not self.enabled:
            if problems is not None:
                problems.append("git cannot be read here; the code of the repositories cannot be observed")
            return []
        from orch.store import observe

        if self.reader is None:
            return observe.observe(store, ref, problems)
        if not self.principal:
            if problems is not None:
                problems.append("git: a host-mode read needs the caller's principal; nothing was observed")
            return []
        # pins are taken by the workspace service at open and on a settings.changed (F7), never here
        return observe.observe(store, ref, problems, backend=_ReaderBackend(self.reader, self))

    def diff(
        self,
        path: Path,
        *,
        name: str,
        branch: str,
        default_branch: str | None,
        remote: str | None,
        context: int = 3,
        max_bytes: int = 262144,
        wall: float | None = None,
        ext: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The ``diff`` core for one repo (section 0.8): ``{branch, base_sha, head_sha, files, patch (base64),
        truncated, origin_urls}``. ``ext`` (H6c): the extra request keys of :mod:`orch.gitread.ops` (``name``, ``pins``,
        ``approvals``, ``pushes``, ``since``, ``head``, ``base``).
        ``wall`` (host mode): the reader's deadline in seconds,
        when shorter than its own.
        Raises :class:`orch.gitread.errors.Unavailable`, or
        :class:`~orch.gitread.errors.RemoteMismatch` when ``remote`` is declared and the observed identity differs."""
        self._need()
        from orch import canon
        from orch.gitread.client import identity_of
        from orch.gitread.errors import RemoteMismatch

        if self.reader is not None:
            got = self.reader.diff(
                path,
                branch=branch,
                default_branch=default_branch,
                context=context,
                max_bytes=max_bytes,
                principal=self.principal,
                **({} if wall is None else {"wall": wall}),
                **({} if not ext else {"ext": ext}),
            )
        else:
            from orch.gitread import local

            got = local.diff(
                path, branch=branch, default_branch=default_branch, context=context, max_bytes=max_bytes, ext=ext
            )
        if remote is not None and not canon.same_repo_identity(remote, identity_of(got["origin_urls"], name)):
            raise RemoteMismatch("the observed origin differs from the declared remote")
        return got


class _ReaderBackend:
    """What ``orch.store.observe.observe`` asks of the host reader: one child per repo for the ref and identity."""

    def __init__(self, reader: Any, git: Git) -> None:
        self.reader = reader
        self.repo_path = git.repo_path
        self.principal = git.principal

    def look(self, path: Path, name: str, ref_name: str) -> tuple[str | None, str]:
        from orch.gitread.client import identity_of

        got = self.reader.observe(path, [ref_name], principal=self.principal)
        return got["refs"].get(ref_name), identity_of(got["origin_urls"], name)


@dataclass(frozen=True)
class Signer:
    """Custody: the person's terminal prompt and keys. Direct mode only."""

    enabled: bool

    def require(self) -> None:
        if not self.enabled:
            raise CapabilityDenied("signer")


@dataclass(frozen=True)
class Capabilities:
    """No defaults: a bare ``Capabilities()`` does not exist. Use :meth:`direct` or :meth:`host`."""

    env: Mapping[str, str] = field(repr=False)
    files: Files
    exec: Exec
    git: Git
    signer: Signer
    clock: Callable[[], float]

    @classmethod
    def direct(cls, env: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time) -> Capabilities:
        """Today's CLI: the same powers handlers had before the capability object. ``env`` ``None``: the process
        environment."""
        return cls(
            env=os.environ if env is None else env,
            files=Files.direct(),
            exec=Exec(enabled=True, base_env=lambda: os.environ),
            git=Git(enabled=True),
            signer=Signer(enabled=True),
            clock=clock,
        )

    @classmethod
    def host(
        cls,
        roots: tuple[str | os.PathLike[str], ...],
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        git: Any = None,
    ) -> Capabilities:
        """What the host's handlers get: files only under ``roots`` (the workspace root and the state directory), no
        client-named paths, cwd or stdin, no exec, no signer, and an environment the caller fixes (empty by default),
        never the client's. ``git``: the workspace's :class:`orch.gitread.client.HostGitReader` (reader children
        under pins); ``None`` denies git. **Build it once per workspace** (it holds the root fds) and ``close()`` it
        when the workspace goes away."""
        return cls(
            env=dict(env or {}),
            files=Files.host(roots),
            exec=Exec(enabled=False, base_env=dict),
            git=Git(enabled=git is not None, reader=git),
            signer=Signer(enabled=False),
            clock=clock,
        )

    def with_principal(self, principal: str) -> Capabilities:
        """The same capabilities for one request: git reads run under ``principal`` (F10 fairness). Shares the file
        capability (its root fds); do not ``close()`` the copy."""
        from dataclasses import replace

        return replace(self, git=replace(self.git, principal=principal))

    def close(self) -> None:
        self.files.close()
