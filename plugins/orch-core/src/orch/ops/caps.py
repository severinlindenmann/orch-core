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

import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class Files:
    """Filesystem access. ``roots`` ``None`` is direct mode (any path); otherwise every path is resolved (symlinks
    followed) and must lie under one root."""

    roots: tuple[Path, ...] | None = None
    cwd_allowed: bool = True
    stdin_allowed: bool = True

    def check(self, path: str | os.PathLike[str]) -> str:
        """``path`` as a string, if allowed. Raises :class:`CapabilityDenied` outside the roots."""
        p = os.fspath(path)
        if self.roots is None:
            return p
        real = Path(os.path.realpath(p))
        if not any(real == r or r in real.parents for r in self.roots):
            raise CapabilityDenied(f"path outside the roots: {p!r}")
        return p

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
        fd = os.open(self.check(path), os.O_RDONLY | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise NotARegularFile(path)
        with os.fdopen(fd, "rb") as f:
            return f.read(limit), st.st_size

    def open_fd(self, path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        return os.open(self.check(path), flags, mode)

    def read_bytes(self, path: str | os.PathLike[str]) -> bytes:
        return Path(self.check(path)).read_bytes()

    def read_text(self, path: str | os.PathLike[str]) -> str:
        return Path(self.check(path)).read_text()

    def write_text(self, path: str | os.PathLike[str], text: str) -> None:
        Path(self.check(path)).write_text(text)

    def mkdir(self, path: str | os.PathLike[str], *, parents: bool = False, mode: int = 0o777) -> None:
        Path(self.check(path)).mkdir(parents=parents, mode=mode, exist_ok=True)

    def unlink(self, path: str | os.PathLike[str]) -> None:
        os.unlink(self.check(path))

    def rmtree(self, path: str | os.PathLike[str]) -> None:
        shutil.rmtree(self.check(path), ignore_errors=True)

    def lexists(self, path: str | os.PathLike[str]) -> bool:
        return os.path.lexists(self.check(path))


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
            files=Files(roots=tuple(Path(os.path.realpath(r)) for r in roots), cwd_allowed=False, stdin_allowed=False),
            exec=Exec(enabled=False),
            git=Git(enabled=False),
            signer=Signer(enabled=False),
            clock=clock,
        )
