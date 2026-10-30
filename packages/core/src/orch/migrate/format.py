"""``format.json`` of a data root and the one open-time rule.

A root is a folder with ``format.json`` (``{"orch": <kind>, "format": N}``): the host state dir (kind ``host``) or a
workspace store ``<project>/.orch`` (kind ``workspace``). :func:`open_root` is the only way in:

* format equals the code's: run, holding a SHARED lock on ``<root>/format.lock`` for as long as the handle lives;
* older: refuse (only ``orch update`` / ``orch migrate`` migrates, never an open);
* newer: refuse;
* no ``format.json``: not an orch root;
* an unfinished update journal: refuse.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from orch import oslock
from orch.ops.errors import OrchError

from .journal import restore_pending, unfinished
from .steps import CURRENT, atomic_write

__all__ = [
    "CURRENT",
    "FORMAT_FILE",
    "LOCK_FILE",
    "RootHandle",
    "init_root",
    "lock_root",
    "open_root",
    "read_format",
    "write_format",
]

FORMAT_FILE = "format.json"
LOCK_FILE = "format.lock"


def read_format(root: Path, kind: str) -> int | None:
    """The root's format number, None if there is no ``format.json``. A file that is damaged, or of another kind,
    raises ``format.missing`` (fail closed)."""
    try:
        with open(Path(root) / FORMAT_FILE, "rb") as f:
            raw = f.read(4097)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _not_a_root(root, exc.strerror or str(exc)) from None
    try:
        doc = json.loads(raw) if len(raw) <= 4096 else None
    except ValueError:
        doc = None
    n = doc.get("format") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or set(doc) != {"orch", "format"} or doc["orch"] != kind:
        raise _not_a_root(root, "format.json is damaged or belongs to another kind of data")
    if type(n) is not int or n < 1:
        raise _not_a_root(root, "format.json is damaged")
    return n


def _not_a_root(root: Path, why: str) -> OrchError:
    return OrchError("format.missing", f"{root} is not orch data ({why})")


def write_format(root: Path, kind: str, n: int) -> None:
    if kind not in CURRENT or n < 1:
        raise ValueError(f"bad format {kind} {n}")
    atomic_write(Path(root) / FORMAT_FILE, (json.dumps({"orch": kind, "format": n}, sort_keys=True) + "\n").encode())


def init_root(root: Path, kind: str) -> None:
    """A new root is born at the code's format. Refuses a root that already has a ``format.json``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if read_format(root, kind) is not None:
        raise OrchError("format.exists", f"{root} already has a format")
    write_format(root, kind, CURRENT[kind])


class RootHandle:
    """An opened root: holds the shared lock until :meth:`close`."""

    def __init__(self, path: Path, kind: str, fmt: int, fd: int) -> None:
        self.path, self.kind, self.format = path, kind, fmt
        self._fd: int | None = fd

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing the descriptor drops the lock

    def __enter__(self) -> RootHandle:
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def lock_root(root: Path, *, exclusive: bool) -> int:
    """The lock fd of ``<root>/format.lock`` (non-blocking). Shared for services, exclusive for a migration."""
    root = Path(root)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(root / LOCK_FILE, flags, 0o600)
    except OSError as exc:
        raise OrchError(
            "migrate.backup_refused",
            f"the lock file in {root.name} cannot be used ({exc.strerror or exc}); it may be a link",
        ) from None
    try:
        oslock.flock(fd, (oslock.LOCK_EX if exclusive else oslock.LOCK_SH) | oslock.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        if exclusive:
            raise OrchError("migrate.running", "orch is running: stop it first (`orch stop`), then try again") from None
        raise OrchError(
            "migrate.running", "orch is being updated right now: wait until it has finished, then try again"
        ) from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _quarantined(env: Mapping[str, str] | None) -> bool:
    """The quarantine marker ``orch update`` writes for its health check: ``<state>/install/quarantine``, a REGULAR
    file (never a link) whose content is the ``update_id`` of the unfinished update journal (hex, optional trailing
    newline). It passes only its own update, and never a journal that has ``restoring`` set."""
    from orch.ops.runtime import state_dir

    from .journal import read_journal

    try:
        doc = read_journal(env)
        if doc is None or doc.get("restoring") or not isinstance(doc.get("update_id"), str):
            return False
        path = state_dir(os.environ if env is None else env) / "install" / "quarantine"
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            raw = os.read(fd, 257)
        finally:
            os.close(fd)
    except (OSError, OrchError):
        return False
    return len(raw) <= 256 and raw.decode("utf-8", "replace").strip() == doc["update_id"]


def _interrupted(env: Mapping[str, str] | None, quarantine_ok: bool) -> bool:
    return unfinished(env) and not (quarantine_ok and _quarantined(env))


def open_root(
    path: Path, kind: str, env: Mapping[str, str] | None = None, *, quarantine_ok: bool = False
) -> RootHandle:
    """``quarantine_ok`` is for the service that ``orch update`` starts for its health check: it passes an unfinished
    update ONLY while the quarantine marker exists. Every other caller is refused by an unfinished journal."""
    path = Path(path)
    if kind not in CURRENT:
        raise ValueError(f"unknown root kind {kind!r}")
    if msg := restore_pending(path):
        raise OrchError("update.interrupted", msg)
    if _interrupted(env, quarantine_ok):
        raise OrchError("update.interrupted", "an update was interrupted: run `orch update` to finish or roll back")
    if not path.is_dir():
        raise _not_a_root(path, "no such folder")
    fd = lock_root(path, exclusive=False)
    try:
        if _interrupted(env, quarantine_ok):  # re-check under the lock: an update may have started meanwhile
            raise OrchError("update.interrupted", "an update was interrupted: run `orch update` to finish or roll back")
        fmt = read_format(path, kind)
        if fmt is None:
            raise _not_a_root(path, "no format.json")
        if fmt < CURRENT[kind]:
            raise OrchError("format.older", "this data was made by an older orch: run `orch update`")
        if fmt > CURRENT[kind]:
            raise OrchError("format.newer", f"this data was made by a newer orch (format {fmt}): update orch")
    except BaseException:
        os.close(fd)
        raise
    return RootHandle(path, kind, fmt, fd)
