"""Migration steps, the step registry and the helpers a step writes through.

A step goes ``from_n`` to ``to_n`` of one root kind. ``apply`` must be idempotent (a crash can run it again);
``verify`` raises if the result is not as expected. A step changes data only through :class:`StepContext`:

* :meth:`StepContext.write_file`: tmp + fsync + rename;
* :meth:`StepContext.sqlite`: the change and the ``user_version`` bump in one transaction.

Both refuse a **signed** path (:data:`SIGNED_DENY`): a migration never rewrites an event log, a checkpoint, a
revocation list, a card or the remote device list. A new shape of signed data is learned by the reader first
(spec, "Data across updates").
"""

from __future__ import annotations

import fnmatch
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from orch.ops.errors import OrchError

__all__ = [
    "CURRENT",
    "DEFAULT",
    "KINDS",
    "SIGNED_DENY",
    "Registry",
    "Step",
    "StepContext",
    "atomic_write",
    "is_signed_path",
    "register",
]

#: the format number the code writes and reads, per root kind (baseline 1 at the 2.0.0 cut)
CURRENT: dict[str, int] = {"host": 1, "workspace": 1}
KINDS = tuple(CURRENT)

#: path-component globs of signed or chained data (event logs, checkpoints, revocations, cards, the remote device
#: list, key logs, every ``*.jsonl`` append-only log). Any component of a relative path matching one is refused.
SIGNED_DENY: tuple[str, ...] = ("event*", "checkpoint*", "revocations*", "*card*", "devices*", "*.jsonl")


def is_signed_path(rel: str | os.PathLike[str]) -> bool:
    parts = PurePosixPath(Path(rel).as_posix()).parts
    return any(fnmatch.fnmatchcase(p, pat) for p in parts for pat in SIGNED_DENY)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """tmp in the same directory, fsync, rename, fsync of the directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0), mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name != "nt":
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


class StepContext:
    """What a step may touch: files and sqlite DBs below ``root``, never a signed path."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, rel: str) -> Path:
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise OrchError("migrate.signed_path", f"a step may only touch files inside the data folder: {rel}")
        if is_signed_path(rel):
            raise OrchError("migrate.signed_path", f"a step may never change signed data: {rel}")
        return self.root / rel

    def write_file(self, rel: str, data: bytes, mode: int = 0o600) -> None:
        atomic_write(self.path(rel), data, mode)

    def sqlite(self, rel: str, fn: Callable[[sqlite3.Connection], Any], *, user_version: int) -> Any:
        """Run ``fn(conn)`` and set ``PRAGMA user_version`` in ONE transaction: both happen or neither."""
        conn = sqlite3.connect(self.path(rel), isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute(f"PRAGMA user_version = {int(user_version)}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return result
        finally:
            conn.close()


@dataclass(frozen=True)
class Step:
    kind: str
    from_n: int
    to_n: int
    name: str
    apply: Callable[[StepContext], None]
    verify: Callable[[Path], None] | None = None


class Registry:
    """The ordered step list plus the format the code is at. Contiguity is checked when it is built."""

    def __init__(self, steps: Iterable[Step], current: dict[str, int] | None = None) -> None:
        self.current = dict(CURRENT if current is None else current)
        self.steps: list[Step] = list(steps)
        self.check()

    def check(self) -> None:
        for kind, target in self.current.items():
            at = 1  # baseline
            for s in (s for s in self.steps if s.kind == kind):
                if s.from_n != at or s.to_n != at + 1:
                    raise ValueError(
                        f"migration steps of {kind} are not contiguous at {at}: {s.name} {s.from_n}->{s.to_n}"
                    )
                at = s.to_n
            if at != target:
                raise ValueError(f"migration steps of {kind} end at {at}, but the code is at format {target}")
        for s in self.steps:
            if s.kind not in self.current:
                raise ValueError(f"unknown root kind {s.kind!r} in step {s.name}")

    def pending(self, kind: str, at: int) -> list[Step]:
        return [s for s in self.steps if s.kind == kind and s.from_n >= at]


#: the steps this build ships (none: format 1 is the baseline). Step modules (one per kind) are imported just above
#: ``DEFAULT`` and call :func:`register`; ``DEFAULT`` is built, so contiguity checked, at import.
STEPS: list[Step] = []


def register(step: Step) -> Step:
    STEPS.append(step)
    return step


DEFAULT = Registry(STEPS)
