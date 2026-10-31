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
import hashlib
import os
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from orch.layout import STATE_DIR, WORK_DIR
from orch.ops.errors import OrchError

__all__ = [
    "CURRENT",
    "DEFAULT",
    "KINDS",
    "NESTED_PART_ROOTS",
    "PART_RUNTIME",
    "PARTS_DIR",
    "PART_KINDS",
    "PROTECTED_NAMES",
    "PROTECTED_PATHS",
    "SIGNED_DENY",
    "Registry",
    "Step",
    "StepContext",
    "atomic_write",
    "is_signed_path",
    "parts_snapshot",
    "protected_snapshot",
    "is_derived",
    "DERIVED_NAMES",
    "register",
]

#: the format number the code writes and reads, per root kind (baseline 1 at the 2.0.0 cut)
CURRENT: dict[str, int] = {"host": 1, "workspace": 1, "hub": 1, "relay": 1, "publish": 1}
KINDS = tuple(CURRENT)
#: the root kinds that are the data dir of an installed part
PART_KINDS = ("hub", "relay", "publish")
#: Under ``install/parts/<part>/`` live: ``data`` (the part's root, ``install.state.Layout.data``), and the runner's own
#: ``pid`` and ``cmd.json`` (``install.service``). Only the ``data`` dirs are roots of their own, nested INSIDE the host
#: root. The roots are disjoint: the host root's snapshot, backup and restore leave exactly these out (a restore
#: carries the current ones over untouched), and each part root is snapshotted, backed up and restored on its own.
#: The rest of ``install/parts`` belongs to the host root.
NESTED_PART_ROOTS: tuple[str, ...] = tuple(f"install/parts/{p}/data" for p in PART_KINDS)
#: ``pid`` is RUNTIME: a running part (re)writes it, so the host's backup leaves it out and a restore carries the
#: current one over, like the data dirs (a stale pid would orphan the live process). ``cmd.json`` is version-bound
#: config and stays in the backup.
PART_RUNTIME: tuple[str, ...] = tuple(f"install/parts/{p}/pid" for p in PART_KINDS)
#: The folder that holds them: no host migration step may touch it (see :func:`parts_snapshot`).
PARTS_DIR = "install/parts"

#: Signed, chained or key data a migration may never touch (matched on the casefolded, symlink-resolved relative path).
#: ``PROTECTED_PATHS`` are globs on the whole relative path, ``PROTECTED_NAMES`` globs on any one component.
PROTECTED_PATHS: tuple[str, ...] = (
    "events/*", "membership/head.json", "membership/keys/*", "keys/*", "*/keys/*", "local-epochs.json",
    "*/local-epochs.json", "sign-ledger.json", "*/sign-ledger.json", "*/checkpoints/*", "checkpoints/*",
    "hosts/*/heads.sqlite*", "hosts/*/transitions/*", "hosts/*/pending-transitions/*", ".state/applied",
    ".state/rejected/*", ".state/body/*",
    ".state/applied/*", ".state/intents/*",
    ".state/checkpoints/*", ".state/abandoned/*", ".state/pending/*", "land/*/key", "known_hosts", "*/known_hosts",
    "git-pins.json", "*/git-pins.json", "secret-pins.json", "*/secret-pins.json", "counters.json", "*/counters.json",
)  # fmt: skip
PROTECTED_NAMES: tuple[str, ...] = (
    "events", "event*", "checkpoint*", "revocations*", "*card*", "devices*", "*.jsonl", "genesis*", "keys",
    "wsk", "*.key", "*.pem",
)  # fmt: skip
#: kept for callers that named the old list
SIGNED_DENY = PROTECTED_NAMES


def is_signed_path(rel: str | os.PathLike[str]) -> bool:
    folded = PurePosixPath(Path(rel).as_posix().casefold())
    parts = folded.parts
    if any(fnmatch.fnmatchcase(p, pat) for p in parts for pat in PROTECTED_NAMES):
        return True
    return any(fnmatch.fnmatchcase(folded.as_posix(), pat) for pat in PROTECTED_PATHS)


#: the only files under ``.state`` that orch rebuilds (the index and the lock; the heads
#: database is durable evidence, not derived): everything else there is real data
DERIVED_NAMES: tuple[str, ...] = ("index.sqlite*", "lock")


def is_derived(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return len(parts) == 2 and parts[0] == STATE_DIR and any(fnmatch.fnmatchcase(parts[1], g) for g in DERIVED_NAMES)


def protected_snapshot(root: Path) -> dict[str, str | None]:
    """sha256 of every protected file below ``root`` (symlinks are recorded by their target text, never followed)."""
    snap: dict[str, str | None] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if (rel_dir.parts[:1] and rel_dir.parts[0] == WORK_DIR) or rel_dir.as_posix() in NESTED_PART_ROOTS:
            dirnames.clear()  # derived data, and the part roots nested in the host root (their own snapshots)
            continue
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if not is_signed_path(rel):
                continue
            p = Path(dirpath) / name
            if p.is_symlink():
                snap[rel] = "link:" + os.readlink(p)
                continue
            h = hashlib.sha256()
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                snap[rel] = h.hexdigest()
            except OSError:
                snap[rel] = None
    return snap


def parts_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """What a host step must never touch, STRUCTURALLY: the entries of ``<root>/install/parts`` and of each
    ``install/parts/<part>``, by lstat (kind, size, mtime, link target), and the inode of every subfolder (so a part
    data dir that is replaced or moved shows). Never the contents of a part's data tree, and never ``pid``: running
    parts write there. Nothing is read or followed."""
    base = Path(root) / PARTS_DIR
    snap: dict[str, tuple[Any, ...]] = {}

    def entries(d: Path, rel: str) -> list[str]:
        out: list[str] = []
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return out
        for name in names:
            erel = f"{rel}/{name}" if rel else name
            if name == "pid" and rel:
                continue  # the runner's pid file: runtime (see PART_RUNTIME)
            try:
                st = os.lstat(d / name)
            except OSError:
                snap[erel] = ("gone",)
                continue
            if stat.S_ISDIR(st.st_mode):
                snap[erel] = ("dir", st.st_dev, st.st_ino)
                out.append(name)
            else:
                link = os.readlink(d / name) if stat.S_ISLNK(st.st_mode) else None
                snap[erel] = (st.st_mode, st.st_size, st.st_mtime_ns, link)
        return out

    for part in entries(base, ""):
        entries(base / part, part)
    return snap


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """tmp in the same directory, fsync, rename, fsync of the directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}-{secrets.token_hex(4)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, mode)
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
    """What a step may touch: only the paths it declared (``Step.writes``), below ``root``, never a protected path."""

    def __init__(self, root: Path, writes: Iterable[str] = ()) -> None:
        self.root = Path(root)
        self.writes = tuple(writes)

    def path(self, rel: str) -> Path:
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise OrchError("migrate.signed_path", f"a step may only touch files inside the data folder: {rel}")
        if is_signed_path(rel):
            raise OrchError("migrate.signed_path", f"a step may never change signed data: {rel}")
        if p.as_posix() == PARTS_DIR or p.as_posix().startswith(PARTS_DIR + "/"):
            raise OrchError("migrate.signed_path", f"a step may never touch the parts' folders: {rel}")
        target = self.root / rel
        real_root = Path(os.path.realpath(self.root))
        real = Path(os.path.realpath(target))
        try:
            real_rel = real.relative_to(real_root).as_posix()
        except ValueError:
            raise OrchError("migrate.signed_path", f"a step may not leave the data folder: {rel}") from None
        if real_rel != "." and is_signed_path(real_rel):
            raise OrchError("migrate.signed_path", f"a step may never change signed data: {rel}")
        if not any(fnmatch.fnmatchcase(p.as_posix(), g) for g in self.writes):
            raise OrchError("migrate.signed_path", f"the step did not declare that it writes {rel}")
        return target

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
    #: the relative paths or globs this step writes (default deny: nothing else can be written)
    writes: tuple[str, ...] = ()


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
