"""Reader records on disk (orch-v2-host §0.8.6 item 9.10, H12d-2): the file side of :mod:`orch.ops.readers`.

``<state>/hosts/<ws>/readers/<device hex>.json`` (0600, the state directory's UID) holds one reader's current
declaration, verbatim, with what it was admitted as::

    {"v":1, "device_id", "person_id", "dk_hash", "reader", "digest", "decl", "source", "at"}

It is never a secret, and it is never trusted as it is: every reader of a record (:func:`orch.ops.readers.levels`)
verifies it again against the replayed F1 state (the device's certificate, its person) and counts a record that does
not verify as level 0. So these helpers only read and write files; they decide nothing.

**One directory for both modes (H12d-2 judgement call).** 9.4 names ``<workspace>/.state/readers/`` for direct mode.
The durable ``convenience_seen`` mark (#441) set the precedent of one place: in tier A the direct-mode and the host
state directory are one (``orch.ops.runtime.state_dir``), so direct mode writes the same
``<state>/hosts/<ws>/readers/`` the host reads after ``orch host register``, and ``register`` "imports" by verifying
each record against the log (:func:`orch.ops.readers.prune`). It lives outside ``orch.ops`` because op modules reach
files only through ``ctx.caps`` (D-P2-9).
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

__all__ = [
    "RECORD_MAX",
    "device_ids",
    "read_record",
    "readers_dir",
    "relay_level",
    "record_path",
    "remove_record",
    "write_record",
]

#: bytes of one record file (a declaration is at most 2 KiB; the record adds a few short fields)
RECORD_MAX = 16 * 1024
_HEX32 = re.compile(r"[0-9a-f]{32}")


def readers_dir(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / "readers"


def record_path(state_dir: Path, workspace_id: str, device_hex: str) -> Path:
    if not _HEX32.fullmatch(device_hex):
        raise ValueError("a reader record is named by the device id's 32 hex digits")
    return readers_dir(state_dir, workspace_id) / f"{device_hex}.json"


def device_ids(state_dir: Path | None, workspace_id: str) -> list[str]:
    """The device ids (bare hex) that have a record file, sorted; none when the directory is missing or unreadable
    (no record counts as level 0: fail closed)."""
    if state_dir is None:
        return []
    try:
        names = os.listdir(readers_dir(state_dir, workspace_id))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and _HEX32.fullmatch(n[:-5]))


def read_record(state_dir: Path | None, workspace_id: str, device_hex: str, uid: int | None = None) -> Any | None:
    """The record of one device as stored, or ``None``: missing, a link, not a private regular file of ``uid``, too
    large or not JSON. ``None`` makes the device count as level 0, so any doubt is the safe answer."""
    if state_dir is None:
        return None
    from orch import canon

    try:
        path = record_path(state_dir, workspace_id, device_hex)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            want = os.getuid() if uid is None else uid
            if not stat.S_ISREG(info.st_mode) or info.st_uid != want or stat.S_IMODE(info.st_mode) & 0o077:
                return None
            raw = fh.read(RECORD_MAX + 1)
    except OSError:
        return None
    if len(raw) > RECORD_MAX:
        return None
    try:
        return canon.loads_strict(raw)
    except (canon.JcsError, ValueError, RecursionError):
        return None


def write_record(state_dir: Path, workspace_id: str, uid: int, record: dict[str, Any]) -> None:
    """Write (replace) one record, atomically and durably, 0600 under 0700 directories of ``uid``. Raises on any
    error: a caller that must have the record written refuses its op."""
    from orch.host.custody import ensure_private_dir, write_private

    hosts = Path(state_dir) / "hosts"
    ensure_private_dir(hosts, uid)
    ensure_private_dir(hosts / workspace_id, uid)
    ensure_private_dir(readers_dir(state_dir, workspace_id), uid)
    write_private(record_path(state_dir, workspace_id, record["device_id"]), record)


def remove_record(state_dir: Path, workspace_id: str, device_hex: str) -> bool:
    """Remove one record file (a record that does not verify, at ``register``); ``False`` if it was not there."""
    try:
        os.unlink(record_path(state_dir, workspace_id, device_hex))
    except FileNotFoundError:
        return False
    return True


def relay_level(state_dir: Path | None, workspace_id: str) -> int | None:
    """The relay's last verified reader level (9.8), ``None`` when the workspace never had a relay. Until H12d-3 checks
    ``GET /v2/relay-info``, every pairing counts as level 0: ``hosts/<ws>/relay/link.json`` (``linking`` or
    ``linked``), any ``relay/unlinked-*.json`` (a former link: the reservation of 11A.6 is permanent) or the R6
    membership state ``hosts/<ws>/membership/``. A dangling link counts, and so does a ``relay/`` directory that
    cannot be listed (fail closed). **H12d-3 hook:** read the level stored with the link here."""
    if state_dir is None:
        return None
    base = Path(state_dir) / "hosts" / workspace_id
    if os.path.lexists(base / "membership") or os.path.lexists(base / "relay" / "link.json"):
        return 0
    try:
        names = os.listdir(base / "relay")
    except FileNotFoundError:
        return None
    except OSError:
        return 0  # cannot tell: a reader may exist
    return 0 if any(n.startswith("unlinked-") and n.endswith(".json") for n in names) else None
