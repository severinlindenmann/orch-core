"""The capabilities a handler gets (D-P2-9): environment, files, exec, git, signer, clock.

Handlers never touch ``os.environ``, ``open()`` or ``subprocess`` themselves; they ask ``ctx.caps``. This module is the
**only** place under ``orch.ops`` that does (``tests/ops/test_capability_boundary.py`` pins that).

| Capability | Host (:meth:`Capabilities.host`)        | Direct mode (:meth:`Capabilities.direct`) |
|------------|-----------------------------------------|-------------------------------------------|
| ``env``    | fixed and minimal, never the client's   | the CLI's environment                     |
| ``files``  | only under the roots; no cwd, no stdin  | any path the CLI names; cwd; stdin        |
| ``exec``   | none (raises :class:`CapabilityDenied`) | the subprocess runner (``verify.cmd``)    |
| ``git``    | none until the reader launcher (H3b1)   | the in-process reader (``store.observe``) |
| ``signer`` | none                                    | custody                                   |
| ``clock``  | host clock                              | system clock                              |

Reading D-P2-9 strictly (an ambiguity, the stricter side taken): in host mode ``files`` is limited to the roots even
for reads of client-named paths (the client sends bytes through ``ctx.files`` instead), the working directory and
stdin are denied, and ``exec`` and ``git`` are denied outright rather than "restricted".
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WHOLE_FILE_LIMIT = 64 << 20  # host mode: read_bytes and read_text refuse a larger file

__all__ = ["CapabilityDenied", "Capabilities", "Exec", "Files", "Git", "Signer", "open_tty"]


class CapabilityDenied(PermissionError):
    """The handler asked for a power its context does not have (host mode). An ``OSError``, so the call sites that
    already treat "cannot run" or "cannot read" as a refusal keep doing so."""

    def __init__(self, what: str) -> None:
        super().__init__(f"{what}: not available to a handler in this mode")
        self.what = what


def open_tty(flags: int) -> int:
    """Open the person's own terminal (``/dev/tty``) as a file descriptor. The terminal is the custody prompt's
    channel, not a file capability; in host mode the audit hook refuses it (outside every root)."""
    return os.open("/dev/tty", flags | os.O_NOCTTY)


class Files:
    """Filesystem access.

    Direct mode (``roots`` ``None``): any path, the plain ``os``/``pathlib`` calls, as before.

    Host mode: **one interpretation of a path, and the check is the open.** The root directories are opened once, as
    directory file descriptors, when the capability is built. A path must be absolute and start with the same
    components as a root (a plain comparison of components: no ``realpath``, no ``normpath``, no case folding). The
    rest is walked component by component with ``dir_fd`` and ``O_NOFOLLOW``, so no symlink is followed, and ``..``,
    ``.``, empty components, NUL bytes and relative paths are refused. There is no separate "validate, then open" step
    for a swap to land in: a symlink swapped in at any moment is simply refused by the kernel. Stricter than the
    D-P2-9 text: a path that reaches a root only through a symlink (``/var`` for ``/private/var``) is refused; the host
    names its roots by their real paths. The one thing trusted is the root directories themselves, which the host owns.
    """

    def __init__(
        self,
        roots: tuple[Path, ...] | None = None,
        cwd_allowed: bool = True,
        stdin_allowed: bool = True,
    ) -> None:
        self.cwd_allowed = cwd_allowed
        self.stdin_allowed = stdin_allowed
        self.roots = roots
        self._root_fds: list[tuple[tuple[str, ...], int]] = []
        for r in roots or ():
            rp = os.fspath(r)
            if "\0" in rp or not os.path.isabs(rp):
                raise ValueError(f"bad root: {rp!r}")
            parts = tuple(c for c in rp.split("/") if c)
            fd = os.open(rp, os.O_RDONLY | os.O_DIRECTORY)
            self._root_fds.append((parts, fd))

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
        with self._parent(path) as (dfd, name):
            # O_NONBLOCK: a FIFO cannot hang the open. Then only a regular file (or a directory, when one was asked
            # for with O_DIRECTORY) is accepted: no FIFO, socket or device is ever read or written.
            fd = os.open(name if name is not None else ".", flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=dfd)
        try:
            kind = os.fstat(fd).st_mode
            if not (stat.S_ISDIR(kind) if flags & os.O_DIRECTORY else stat.S_ISREG(kind)):
                raise CapabilityDenied(f"not a regular file: {os.fspath(path)!r}")
            os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        return fd

    # -- the capability
    def getcwd(self) -> str:
        if not self.cwd_allowed:
            raise CapabilityDenied("working directory")
        return os.getcwd()

    def read_stdin(self, limit: int) -> bytes:
        """At most ``limit`` bytes of standard input."""
        if not self.stdin_allowed:
            raise CapabilityDenied("stdin")
        stream = getattr(sys.stdin, "buffer", None)
        return stream.read(limit) if stream is not None else sys.stdin.read(limit).encode()

    def read_regular(self, path: str, limit: int) -> tuple[bytes, int]:
        """Up to ``limit`` bytes of a regular file (opened ``O_NONBLOCK``, so a FIFO cannot hang the call) and the
        file's size. Raises ``OSError`` if it cannot be opened and :class:`NotARegularFile` if it is not one."""
        fd = self._open(path, os.O_RDONLY | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise NotARegularFile(path)
        with os.fdopen(fd, "rb") as f:
            return f.read(limit), st.st_size

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
        if self.roots is None:
            return Path(path).read_text()
        return self.read_bytes(path).decode()

    def write_text(self, path: str | os.PathLike[str], text: str) -> None:
        if self.roots is None:
            Path(path).write_text(text)
            return
        with os.fdopen(self._open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666), "w") as f:
            f.write(text)

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

    def rmtree(self, path: str | os.PathLike[str]) -> None:
        if self.roots is None:
            shutil.rmtree(path, ignore_errors=True)
            return
        with self._parent(path) as (dfd, name):
            if name is None:
                raise CapabilityDenied("a root cannot be removed")
            shutil.rmtree(name, ignore_errors=True, dir_fd=dfd)

    def lexists(self, path: str | os.PathLike[str]) -> bool:
        if self.roots is None:
            return os.path.lexists(path)
        try:
            with self._parent(path) as (dfd, name):
                os.lstat(name if name is not None else ".", dir_fd=dfd)
        except OSError:
            return False
        return True


class NotARegularFile(OSError):
    """``read_regular`` met a directory, a FIFO or a device."""


@dataclass(frozen=True)
class Exec:
    """Process spawning (``verify.cmd``). ``enabled`` ``False``: every call raises :class:`CapabilityDenied`.

    ``base_env`` is the environment a spawned command starts from, before the caller's allow-list: the process
    environment in direct mode."""

    enabled: bool = True
    base_env: Callable[[], Mapping[str, str]] = field(default=lambda: os.environ, repr=False)

    def popen(self, argv: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        if not self.enabled:
            raise CapabilityDenied("exec")
        return subprocess.Popen(argv, **kw)

    def run(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        if not self.enabled:
            raise CapabilityDenied("exec")
        return subprocess.run(argv, **kw)

    def child_env(self, extra: Mapping[str, str]) -> dict[str, str]:
        if not self.enabled:
            raise CapabilityDenied("exec")
        return {**self.base_env(), **extra}


@dataclass(frozen=True)
class Git:
    """Reading the linked repositories. Direct mode: the in-process reader (``orch.store.observe``). Host mode: none
    here; H3b1 supplies the reader launcher."""

    enabled: bool = True

    def _need(self) -> None:
        if not self.enabled:
            raise CapabilityDenied("git")

    def head(self, path: Path) -> str | None:
        self._need()
        from orch.store import observe

        return observe.head(path)

    def dirty(self, path: Path) -> str | None:
        self._need()
        from orch.store import observe

        return observe.dirty(path)

    def run(self, path: Path, *args: str, strip: bool = True) -> str | None:
        self._need()
        from orch.store import observe

        return observe.git(path, *args, strip=strip)

    def identity(self, path: Path, name: str) -> str:
        self._need()
        from orch.store import observe

        return observe.repo_identity(path, name)

    def observe(self, store: Any, ref: str, problems: list[str] | None = None) -> list[dict[str, Any]]:
        self._need()
        from orch.store import observe

        return observe.observe(store, ref, problems)


@dataclass(frozen=True)
class Signer:
    """Custody: the person's terminal prompt and keys. Direct mode only."""

    enabled: bool = True

    def require(self) -> None:
        if not self.enabled:
            raise CapabilityDenied("signer")


@dataclass(frozen=True)
class Capabilities:
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    files: Files = field(default_factory=Files)
    exec: Exec = field(default_factory=Exec)
    git: Git = field(default_factory=Git)
    signer: Signer = field(default_factory=Signer)
    clock: Callable[[], float] = time.time

    @classmethod
    def direct(cls, env: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time) -> Capabilities:
        """Today's CLI: the same powers handlers had before the capability object. ``env`` ``None``: the process
        environment."""
        return cls(env=os.environ if env is None else env, clock=clock)

    @classmethod
    def host(
        cls,
        roots: tuple[str | os.PathLike[str], ...],
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> Capabilities:
        """What the host's handlers get: files only under ``roots`` (the workspace root and the state directory),
        no cwd, no stdin, no exec, no git (until H3b1), no signer, and an environment the caller fixes (empty by
        default), never the client's."""
        return cls(
            env=dict(env or {}),
            files=Files(roots=tuple(Path(r) for r in roots), cwd_allowed=False, stdin_allowed=False),
            exec=Exec(enabled=False),
            git=Git(enabled=False),
            signer=Signer(enabled=False),
            clock=clock,
        )
