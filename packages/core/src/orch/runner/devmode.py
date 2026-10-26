"""The dev-only foreground runner admission (dev and tests; never a service).

The host admits peer class ``runner`` from the runner unit's service-manager main PID (D-P2-25,
:func:`orch.host.runners.runner_identity`). A dev or test setup that runs host and runner as separate foreground
processes has no service manager, so that check can never pass. This module is the narrow seam for that setup:

* ``orch runner run --foreground`` **announces** itself: :func:`announce` writes ``<run>/term/<uid>/dev-runner.json``
  (0600 in the runner's 0700 terminal directory) with its PID and the process's birth time, and :func:`withdraw`
  removes it at exit.
* Only a host started as ``orch host run --foreground --dev-runner`` (``Host(dev_runner=True)``) consults it:
  :func:`verify` admits a connecting process when the kernel's peer UID is the host UID, the file is a private regular
  file of that UID (no link) in a private directory, it names that UID and the peer's PID, and that PID is still the
  process born at the recorded time (a stale file whose PID was reused is refused).
* Never for a service run directory (``/Users/Shared/orch/run``, ``/run/orch``: :func:`is_service_run_dir`), never
  for another UID. Every failure denies.

Tier A: the runner UID is the host UID, so (as with the main-PID check) this is fault separation and hygiene, not a
boundary: any process of the host's UID could write the file. It exists so that a misdirected or stale process is not
taken for the runner, not to keep a hostile same-UID process out.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
from pathlib import Path
from typing import Any

__all__ = ["FILE", "SERVICE_RUN_ROOTS", "announce", "is_service_run_dir", "path_for", "verify", "withdraw"]

FILE = "dev-runner.json"
#: the service run directories of D-P2-2 (macOS, Linux): never a dev runner there, whatever the platform
SERVICE_RUN_ROOTS = (Path("/Users/Shared/orch/run"), Path("/run/orch"))


def path_for(run_dir: Path, uid: int) -> Path:
    return Path(run_dir) / "term" / str(uid) / FILE


def is_service_run_dir(run_dir: Path) -> bool:
    """``run_dir`` is (or lies inside) a service run directory. Decided by identity, not spelling: by path (as written
    and resolved; case-folded on macOS, whose default file system ignores case, so a case-sensitive volume can only
    refuse more) and by ``(st_dev, st_ino)`` of every existing ancestor of the resolved path against every service
    root that exists (a link, another case or a second mount of the same directory)."""
    seen = {Path(os.path.normpath(os.path.abspath(run_dir)))}
    with contextlib.suppress(OSError, ValueError):
        seen.add(Path(os.path.realpath(run_dir)))
    roots = set(SERVICE_RUN_ROOTS)
    for root in SERVICE_RUN_ROOTS:
        with contextlib.suppress(OSError, ValueError):
            roots.add(Path(os.path.realpath(root)))

    def fold(p: Path) -> Path:
        return Path(str(p).casefold()) if sys.platform == "darwin" else p

    if any(fold(p) == fold(r) or fold(p).is_relative_to(fold(r)) for p in seen for r in roots):
        return True
    ids = set()
    for root in roots:
        with contextlib.suppress(OSError, ValueError):
            st = os.stat(root)
            ids.add((st.st_dev, st.st_ino))
    if not ids:
        return False
    for p in seen:
        for ancestor in (p, *p.parents):
            with contextlib.suppress(OSError, ValueError):
                st = os.stat(ancestor)
                if (st.st_dev, st.st_ino) in ids:
                    return True
    return False


def _private_dir(directory: Path, uid: int) -> None:
    top = os.lstat(directory.parent)
    if not stat.S_ISDIR(top.st_mode) or top.st_uid not in (0, uid) or stat.S_IMODE(top.st_mode) & 0o022:
        raise OSError(errno.EPERM, f"{directory.parent}: expected a directory of root or UID {uid}")
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise OSError(errno.EPERM, f"{directory}: expected a 0700 directory of UID {uid}")


def announce(run_dir: Path, uid: int) -> Path:
    """Write this process's announcement (PID, birth time, UID) and return its path. The directories are created as
    the terminal socket's are (``<run>/term`` 0711, ``<run>/term/<uid>`` 0700)."""
    from orch.host.custody import write_private

    from . import procs

    if is_service_run_dir(run_dir):
        raise OSError(errno.EPERM, f"{run_dir} is a service run directory: no dev runner there")
    path = path_for(run_dir, uid)
    with contextlib.suppress(FileExistsError):
        os.mkdir(path.parent.parent, 0o711)
    with contextlib.suppress(FileExistsError):
        os.mkdir(path.parent, 0o700)
    _private_dir(path.parent, uid)
    pid = os.getpid()
    birth = procs.birth(pid)
    if birth is None:
        raise OSError(errno.ESRCH, "this process's birth time cannot be read")
    write_private(path, {"v": 1, "pid": pid, "birth": birth, "uid": uid})
    return path


def withdraw(path: Path, pid: int) -> None:
    """Remove the announcement if it is still ``pid``'s (a newer runner's file is left alone)."""
    from orch.host.custody import read_json
    from orch.host.errors import HostError

    try:
        doc = read_json(path, os.getuid(), limit=4096)
    except HostError:
        return
    if isinstance(doc, dict) and doc.get("pid") == pid:
        with contextlib.suppress(OSError):
            path.unlink()


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def verify(run_dir: Path, host_uid: int, credentials: Any) -> bool:
    """Is the connecting process (kernel peer ``credentials``) the runner announced in ``run_dir``? Fail closed."""
    try:
        from orch.host.custody import read_json

        from . import procs

        uid, pid = credentials.uid, credentials.pid
        if not _int(uid) or not _int(pid) or uid != host_uid or pid < 2 or is_service_run_dir(run_dir):
            return False
        path = path_for(run_dir, host_uid)
        _private_dir(path.parent, host_uid)
        doc = read_json(path, host_uid, limit=4096)  # O_NOFOLLOW, regular, owner, 0600
        if not isinstance(doc, dict) or doc.get("v") != 1:
            return False
        if doc.get("uid") != host_uid or doc.get("pid") != pid or not _int(doc.get("birth")):
            return False
        found = procs.info(pid)
        return found is not None and not found.zombie and found.uid == host_uid and found.birth == doc["birth"]
    except Exception:  # noqa: BLE001 - anything unexpected denies
        return False
