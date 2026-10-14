"""The durable ``convenience_seen`` mark and the cut-tail scans (#441): the file side of :mod:`orch.ops.readers`.

It lives outside ``orch.ops`` because op modules reach files only through ``ctx.caps`` (D-P2-9,
``tests/ops/test_capability_boundary.py``); these helpers run under the store lock or in the host, like the rest of
the store. :mod:`orch.ops.readers` re-exports every name, so callers (and tests that patch them there) are unchanged.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

__all__ = [
    "SEEN_FILE",
    "abandoned_have_convenience",
    "canonical_lines_have_convenience",
    "convenience_seen",
    "ensure_convenience_mark",
    "seen_path",
]


def _lf_lines(data: bytes) -> list[bytes]:
    """``data`` split after each LF, terminators kept; a last piece without its LF stays without it."""
    parts = data.split(b"\n")
    return [p + b"\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def canonical_lines_have_convenience(data: bytes) -> bool:
    """For a cut tail or an archived one (Opus #454 L4): a line counts only when ``canon.parse_event_line`` accepts it
    (exact canonical JSON, one LF) and its ``auth`` is ``"convenience"``. A torn, unparseable or non-canonical line
    never counts there: it is not an event any reader accepted. (Verifying the signature too is a follow-up.)"""
    from orch import canon

    # LF only, terminators kept: a bare CR is not a line boundary in an event log (Codex #454)
    for line in _lf_lines(data):
        if b"convenience" not in line:
            continue
        try:
            ev = canon.parse_event_line(line)
        except Exception:  # noqa: BLE001 - not an event line: never counts here
            continue
        if ev.get("auth") == "convenience":
            return True
    return False


#: bytes read per archived tail (a cut tail is small; a bigger file counts as convenience: fail closed)
_ABANDONED_MAX = 64 * 1024 * 1024


def abandoned_have_convenience(state_dir: Path | None) -> bool:
    """A cut tail in ``<root>/.state/abandoned/*.jsonl`` holds a convenience event: a canonical event line with
    ``auth: "convenience"`` (:func:`canonical_lines_have_convenience`). Any error reading counts (fail closed)."""
    if state_dir is None:
        return False
    ab = Path(state_dir) / "abandoned"
    try:
        names = sorted(n for n in os.listdir(ab) if n.endswith(".jsonl"))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for name in names:
        try:
            fd = os.open(ab / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(_ABANDONED_MAX + 1)
        except OSError:
            return True
        if len(data) > _ABANDONED_MAX or canonical_lines_have_convenience(data):
            return True
    return False


SEEN_FILE = "convenience_seen.json"


def seen_path(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / SEEN_FILE


def convenience_seen(state_dir: Path | None, workspace_id: str) -> bool:
    """For refusals: the durable mark is set. Conservative: anything at the path, or any error looking, counts as
    seen. Never used to decide whether the mark still has to be written (:func:`ensure_convenience_mark`)."""
    if state_dir is None:
        return False
    try:
        os.lstat(seen_path(state_dir, workspace_id))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def ensure_convenience_mark(state_dir: Path, workspace_id: str, host_uid: int, *, clock: Any = time.time) -> None:
    """Make sure the mark is persisted: a regular file at the path, else written now (idempotent, never cleared).
    Raises on any error (a lookup error, something else at the path, a failed write): an append or a cut that needs
    the mark must not go ahead."""
    from orch.host.custody import ensure_private_dir, unsafe, write_private

    path = seen_path(state_dir, workspace_id)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISREG(info.st_mode):
            return
        raise unsafe(f"{path}: not a regular file")
    hosts = Path(state_dir) / "hosts"
    ensure_private_dir(hosts, host_uid)
    ensure_private_dir(hosts / workspace_id, host_uid)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(clock())))
    write_private(path, {"v": 1, "seen": True, "at": stamp})
