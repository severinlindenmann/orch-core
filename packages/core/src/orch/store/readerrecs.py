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
    "LEVEL_FILE",
    "clear_relay_level",
    "read_relay_level",
    "relay_level",
    "relay_level_path",
    "record_path",
    "remove_record",
    "reset_records",
    "write_record",
    "write_relay_level",
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


#: the relay's last verified reader level (H12d-3, §0.8.6 item 9.8), next to ``link.json``: ``{"v":1, "reader":N,
#: "at_ms":T}`` (0600). Written at link step 1 (in the reservation's critical section, only while no ``link.json``
#: exists) and at every R1 login (:func:`orch.host.relaylink.observe_relay_level`). It survives an unlink (the
#: renamed ``unlinked-*.json`` keeps counting with its last level); a relink writes the new relay's level.
LEVEL_FILE = "reader.json"
_LEVEL_MAX = 2**31 - 1


def relay_level_path(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / "relay" / LEVEL_FILE


def read_relay_level(state_dir: Path | None, workspace_id: str, uid: int | None = None) -> int | None:
    """The stored level, or ``None``: missing, a link, not a private regular file of ``uid``, or any other shape than
    ``{"v":1, "reader":uint31, "at_ms":uint}``. ``None`` makes a linked relay count as 0 (fail closed)."""
    if state_dir is None:
        return None
    from orch import canon

    try:
        fd = os.open(
            relay_level_path(state_dir, workspace_id), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            want = os.getuid() if uid is None else uid
            if not stat.S_ISREG(info.st_mode) or info.st_uid != want or stat.S_IMODE(info.st_mode) & 0o077:
                return None
            raw = fh.read(4097)
        doc = canon.loads_strict(raw) if len(raw) <= 4096 else None
    except (OSError, canon.JcsError, ValueError, RecursionError):
        return None
    if type(doc) is not dict or set(doc) != {"v", "reader", "at_ms"} or type(doc["v"]) is not int or doc["v"] != 1:
        return None
    reader, at = doc["reader"], doc["at_ms"]
    if type(reader) is not int or not 0 <= reader <= _LEVEL_MAX or type(at) is not int or at < 0:
        return None
    return reader


def write_relay_level(state_dir: Path, workspace_id: str, uid: int, level: int, at_ms: int) -> None:
    """Write (replace) the relay's level atomically, 0600 under 0700 directories of ``uid``. Raises on any error."""
    from orch.host.custody import ensure_private_dir, write_private

    if type(level) is not int or not 0 <= level <= _LEVEL_MAX or type(at_ms) is not int or at_ms < 0:
        raise ValueError("a relay reader level is a uint31 with a time")
    hosts = Path(state_dir) / "hosts"
    ensure_private_dir(hosts, uid)
    ensure_private_dir(hosts / workspace_id, uid)
    ensure_private_dir(hosts / workspace_id / "relay", uid)
    write_private(relay_level_path(state_dir, workspace_id), {"v": 1, "reader": level, "at_ms": at_ms})


def clear_relay_level(state_dir: Path, workspace_id: str) -> None:
    """Remove a stored level (a link whose relay-info was not read: it counts as 0). Raises unless it is gone."""
    try:
        os.unlink(relay_level_path(state_dir, workspace_id))
    except FileNotFoundError:
        pass


def reset_records(state_dir: Path, workspace_id: str) -> list[str]:
    """``orch host readers reset`` (H12d-3, 9.10): remove every record file (``<32 hex>.json``) of the workspace and
    return the device ids (bare hex) removed. A missing directory is nothing to reset; anything else that keeps the
    directory from being listed (a link, a file, no permission) raises ``OSError``, and so does a record that cannot
    be removed (#555 review F4: the operator sees it, never a silent success). Fail closed: every device then counts
    as level 0."""
    d = readers_dir(state_dir, workspace_id)
    try:
        info = os.lstat(d)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{d} is not a directory")
    names = os.listdir(d)  # raises: never the swallowing device_ids()
    out = []
    for hexid in sorted(n[:-5] for n in names if n.endswith(".json") and _HEX32.fullmatch(n[:-5])):
        if remove_record(state_dir, workspace_id, hexid):
            out.append(hexid)
    return out


def relay_level(state_dir: Path | None, workspace_id: str, uid: int | None = None) -> int | None:
    """The relay's last verified reader level (9.8), ``None`` when the workspace never had a relay. A relay exists
    while ``hosts/<ws>/relay/link.json`` (``linking`` or ``linked``), any ``relay/unlinked-*.json`` (a former link:
    the reservation of 11A.6 is permanent) or the R6 membership state ``hosts/<ws>/membership/`` exists; a dangling
    link counts, and so does a ``relay/`` directory that cannot be listed (fail closed). Its level is the stored one
    (:data:`LEVEL_FILE`, H12d-3: ``GET /v2/relay-info`` at link step 1 and every R1 login), 0 when there is none or
    it cannot be read (fail closed)."""
    if state_dir is None:
        return None
    base = Path(state_dir) / "hosts" / workspace_id
    if not (os.path.lexists(base / "membership") or os.path.lexists(base / "relay" / "link.json")):
        try:
            names = os.listdir(base / "relay")
        except FileNotFoundError:
            return None
        except OSError:
            return 0  # cannot tell: a reader may exist
        if not any(n.startswith("unlinked-") and n.endswith(".json") for n in names):
            return None
    return read_relay_level(state_dir, workspace_id, uid) or 0
