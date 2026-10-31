"""The open-time rule for a part's data dir (hub, relay, publish).

Stdlib only; one file copied byte for byte into the three packages (a guard test in each
compares them). It is the part-side twin of ``orch.migrate.format.open_root`` in orch-core, with
the same codes and wording.

A data dir has ``format.json`` (``{"orch": <kind>, "format": N}``, written by ``orch install``)
and ``format.lock``, a SHARED ``flock`` the service holds for its whole life (``orch update``
takes it EXCLUSIVE: it cannot start under a running service, and a service cannot start under an
update). :func:`open_data` refuses, in this order:

* a pending restore (its sidecar ``<parent>/.<name>.restore/record.json``): ``update.interrupted``;
* an unfinished update, when ``ORCH_UPDATE_JOURNAL`` names the update journal:
  ``update.interrupted``;
* no such folder: ``format.missing``;
* the lock is held exclusive (an update runs): ``update.busy``;
* no ``format.json`` (or a damaged one, a link, one of another kind): ``format.missing``;
* an older / a newer format: ``format.older`` / ``format.newer``.

POSIX only. Keep the returned :class:`Handle` referenced while serving: closing it (or collecting
it) drops the lock.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from types import TracebackType
from typing import Self

__all__ = ["DataFormatError", "Handle", "check_update", "open_data"]

FORMAT_FILE = "format.json"
LOCK_FILE = "format.lock"
JOURNAL_ENV = "ORCH_UPDATE_JOURNAL"

_PHASES = ("stopped", "backed-up", "installed", "committed")
_MIGRATED = re.compile(r"migrated:[^\x00-\x1f]{1,4096}")
_JOURNAL_LIMIT = 1 << 20
# a tuple: the bare `except A, B` form is Python 3.14 only, and the floor is 3.11
_RECORD_ERRORS = (OSError, ValueError, AttributeError)
_INTERRUPTED = "an update was interrupted: run `orch update` to finish or roll back"


class DataFormatError(Exception):
    """A refusal to open the data dir. ``code`` is stable (``update.interrupted``,
    ``update.busy``, ``format.missing``, ``format.older``, ``format.newer``); ``message`` is a
    plain sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _restore_pending(path: str) -> str | None:
    root = os.path.realpath(path)  # a symlinked root: the sidecar sits beside the real folder
    parent, name = os.path.split(root)
    rec = os.path.join(parent, f".{name}.restore", "record.json")
    if not os.path.lexists(rec):
        return None
    try:
        with open(rec, "rb") as f:
            aside = json.loads(f.read(1 << 16)).get("aside")
    except _RECORD_ERRORS:
        aside = None
    if isinstance(aside, str):
        base = os.path.basename(aside.rstrip("/"))
        where = f"your data is in {base!a}"
    else:
        where = "see the folders beside it"
    return (
        f"orch was stopped while putting back a backup; {where}; "
        f"run `orch migrate --apply` or rename it back to {name!a}"
    )


def _valid_phase(phase: object) -> bool:
    return isinstance(phase, str) and (phase in _PHASES or _MIGRATED.fullmatch(phase) is not None)


def _unfinished(env: Mapping[str, str]) -> bool:
    """The same decision as ``orch.migrate.journal.unfinished``. An unreadable or malformed
    journal counts as unfinished (fail closed); a missing one is no update; else any phase but
    ``committed``, or ``restoring`` set."""
    path = env.get(JOURNAL_ENV)
    if not path:
        return False  # not started by `orch install` (by hand, in tests): no journal to look at
    try:
        with open(path, "rb") as f:
            raw = f.read(_JOURNAL_LIMIT + 1)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        doc = json.loads(raw) if len(raw) <= _JOURNAL_LIMIT else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or doc.get("v") != 1 or not _valid_phase(doc.get("phase")):
        return True
    return doc["phase"] != "committed" or bool(doc.get("restoring"))


def check_update(path: str | os.PathLike[str], env: Mapping[str, str] = os.environ) -> None:
    """The two refusals that need no lock (a pending restore, an unfinished update): for a
    command that works on a data dir that does not exist yet (a restore onto a fresh server)."""
    if msg := _restore_pending(os.fspath(path)):
        raise DataFormatError("update.interrupted", msg)
    if _unfinished(env):
        raise DataFormatError("update.interrupted", _INTERRUPTED)


def _not_data(path: str, why: str) -> DataFormatError:
    return DataFormatError("format.missing", f"{path} is not orch data ({why})")


def _read_format(path: str, kind: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.path.join(path, FORMAT_FILE), flags)
    except FileNotFoundError:
        raise DataFormatError(
            "format.missing",
            f"this folder has no format.json: run `orch install {kind}` again",
        ) from None
    except OSError as exc:  # ELOOP: a link
        why = f"format.json cannot be opened ({exc.strerror or exc}); it may be a link"
        raise _not_data(path, why) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _not_data(path, "format.json is not a regular file")
        raw = os.read(fd, 4097)
    except OSError as exc:
        raise _not_data(path, exc.strerror or str(exc)) from None
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw) if len(raw) <= 4096 else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or set(doc) != {"orch", "format"} or doc["orch"] != kind:
        raise _not_data(path, "format.json is damaged or belongs to another kind of data")
    n = doc["format"]
    if type(n) is not int or n < 1:
        raise _not_data(path, "format.json is damaged")
    return n


class Handle:
    """An opened data dir: holds the shared lock until :meth:`close`."""

    def __init__(self, path: str, kind: str, fmt: int, fd: int) -> None:
        self.path, self.kind, self.format = path, kind, fmt
        self._fd: int | None = fd

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing the descriptor drops the lock

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        et: type[BaseException] | None,
        ev: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def open_data(
    path: str | os.PathLike[str],
    kind: str,
    current: int,
    env: Mapping[str, str] = os.environ,
) -> Handle:
    """Open the data dir ``path`` of part ``kind`` whose code is at format ``current``, or raise
    :class:`DataFormatError`. The returned handle holds the SHARED lock of
    ``<path>/format.lock``: keep it for as long as the data is in use."""
    try:
        import fcntl
    except ImportError:
        raise RuntimeError(
            "orch parts run on Linux and macOS only: the data format lock needs POSIX flock",
        ) from None
    path = os.fspath(path)
    check_update(path, env)
    if not os.path.isdir(path):
        raise _not_data(path, "no such folder")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.path.join(path, LOCK_FILE), flags, 0o600)
    except OSError as exc:
        why = f"the lock file cannot be used ({exc.strerror or exc}); it may be a link"
        raise _not_data(path, why) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DataFormatError(
                "update.busy",
                "orch is being updated right now: wait until it has finished, then try again",
            ) from None
        if _unfinished(env):  # re-check under the lock: an update may have started meanwhile
            raise DataFormatError("update.interrupted", _INTERRUPTED)
        fmt = _read_format(path, kind)
        if fmt < current:
            raise DataFormatError(
                "format.older",
                "this data was made by an older orch: run `orch update`",
            )
        if fmt > current:
            raise DataFormatError(
                "format.newer",
                f"this data was made by a newer orch (format {fmt}): update orch",
            )
    except BaseException:
        os.close(fd)
        raise
    return Handle(path, kind, fmt, fd)
