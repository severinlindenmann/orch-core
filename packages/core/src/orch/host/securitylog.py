"""The host's security audit log (§0.11): ``<state>/host/audit-security.jsonl``, one JSON object per line.

Read traffic never rotates it away. This is the first writer (H4a2: ``artifact.mismatch``, a file changed or swapped
behind the log); the other entries the §0.11 table names (``human.*``, writes, ``peer.denied``, control ops) and the
rotation of ``audit.jsonl`` belong to a later group. A line is appended with ``O_APPEND`` to a file opened without
following a symlink and checked like the host's other private files (a regular file of the host UID with one link
and no group or other bits), in the host-owned 0700 directory. Writing it never changes an op's answer.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any

from .custody import ensure_private_dir

__all__ = ["NAME", "record"]

NAME = "audit-security.jsonl"
LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()


def record(state_dir: Path, host_uid: int, at: float, event: str, **fields: Any) -> None:
    """Append ``{at, event, **fields}``; a failure is logged, never raised."""
    line = json.dumps({"at": round(float(at), 3), "event": event, **fields}, sort_keys=True, separators=(",", ":"))
    try:
        directory = ensure_private_dir(Path(state_dir) / "host", host_uid)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        with _LOCK:
            fd = os.open(directory / NAME, flags, 0o600)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != host_uid or st.st_nlink != 1 or st.st_mode & 0o077:
                    raise OSError(f"{NAME} is not a private regular file of the host")
                os.write(fd, line.encode() + b"\n")
            finally:
                os.close(fd)
    except Exception as exc:  # noqa: BLE001 - the audit line is a record, not a gate
        LOG.error("audit-security: could not write %s: %s", event, exc)
