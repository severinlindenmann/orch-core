"""Adding a reader to a workspace whose history holds ``auth: "convenience"`` events (orch-v2-host §0.8.6 item 1).

Owner decision, 12 Oct 2026: such a workspace refuses ops that add a reader of its log (``member.add`` now; relay
pairing and device ops when they exist) with ``sign.convenience_readers``, in host and direct mode, until the new
readers understand the value. Switching back to terminal signing does not help: the new reader must still parse the
convenience events already in the history. A workspace with no convenience events yet may switch convenience off and
then add the reader.

**The durable mark (#441, orch-v2-host §0.8.6 item 9 / H12d 9.7).** ``<state>/hosts/<ws>/convenience_seen.json``
records that a convenience event once landed in this workspace. It is written at the append choke point (the host's
:func:`orch.host.human.append_signed` and direct mode's ``Human`` append) before the first convenience event is written,
and whenever a history scan finds one. Nothing clears it: an owner repair (``abandon_tail``, then ``restore``) can
drop convenience events from the merged history, but the record files and any records repo still hold them. Every
reader-adding check asks the mark as well as the merged history. Reading fails closed: any doubt (an unsafe or
unreadable file, a link in its place) counts as seen.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orch.ops.errors import OrchError
from orch.store.convmark import (  # the file side lives in the store (D-P2-9); re-exported here
    SEEN_FILE,
    abandoned_have_convenience,
    canonical_lines_have_convenience,
    convenience_seen,
    ensure_convenience_mark,
    seen_path,
)

__all__ = [
    "READER_OPS",
    "SEEN_FILE",
    "abandoned_have_convenience",
    "canonical_lines_have_convenience",
    "convenience_seen",
    "ensure_convenience_mark",
    "events_have_convenience",
    "has_convenience_events",
    "line_is_convenience",
    "lines_have_convenience",
    "refuse_new_reader",
    "seen_or_history",
    "seen_path",
]

#: ops that add a reader of the workspace's log; the host (:mod:`orch.host.convenience`) and the op handlers use this
#: one set. ``relay.link`` (a relay link) and ``relay.pair`` (the host's ``approved`` answer to a pairing, and any
#: member list that adds a device) are host ops of the relay bridge (orch-v2-host §11A.6)
READER_OPS = frozenset({"member.add", "relay.link", "relay.pair"})


def has_convenience_events(store: Any) -> bool:
    """True when any event of any log carries ``auth: "convenience"``, or a tail cut by ``abandon_tail`` holds one
    (``<root>/.state/abandoned/``: events that left the merged view; a workspace from before the mark gets it this
    way). Reads every log: only for rare ops."""
    if any(ev.get("auth") == "convenience" for _, ev in store.merged_events()):
        return True
    return abandoned_have_convenience(getattr(store, "state_dir", None))


def events_have_convenience(events: Iterable[dict[str, Any]]) -> bool:
    return any(isinstance(ev, dict) and ev.get("auth") == "convenience" for ev in events)


def _pairs_auth(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if any(k == "auth" and v == "convenience" for k, v in pairs):  # a duplicate key never hides the value
        out["auth"] = "convenience"
    return out


def line_is_convenience(line: bytes, *, unparseable: bool = True) -> bool:
    """One line of an event log is an event with ``auth: "convenience"``. The parse is lenient on purpose (``\\u``
    escapes, duplicate keys, any order or spacing): stricter than any reader would be is the safe side here. A line
    that does not parse as a JSON object counts as one when ``unparseable`` (fail closed); otherwise only when it
    names the value at all. Never raises (a nesting bomb is a ``RecursionError``, caught)."""
    line = line.rstrip(b"\r\n")
    if not line.strip():
        return False
    try:
        ev = json.loads(line, object_pairs_hook=_pairs_auth)
    except Exception:  # noqa: BLE001 - ValueError, UnicodeDecodeError, RecursionError: any doubt
        return unparseable or b"convenience" in line
    if not isinstance(ev, dict):
        return unparseable or b"convenience" in line
    return ev.get("auth") == "convenience"


def lines_have_convenience(data: bytes, *, unparseable: bool = True) -> bool:
    return any(line_is_convenience(line, unparseable=unparseable) for line in data.split(b"\n"))


def seen_or_history(store: Any, state_dir: Path | None, workspace_id: str, host_uid: int | None = None) -> bool:
    """The mark, else a scan of the merged history (and the cut tails); a scan that finds one sets the mark (best
    effort: the answer is already "seen", so a failed write changes nothing now)."""
    if convenience_seen(state_dir, workspace_id):
        return True
    if not has_convenience_events(store):
        return False
    if state_dir is not None:
        with contextlib.suppress(Exception):
            ensure_convenience_mark(state_dir, workspace_id, os.getuid() if host_uid is None else host_uid)
    return True


def refuse_new_reader(store: Any, what: str, state_dir: Path | None = None) -> None:
    """``sign.convenience_readers`` when the durable mark is set or the merged history holds convenience events."""
    wid = store.state.workspace.workspace_id if state_dir is not None else ""
    if seen_or_history(store, state_dir, str(wid)):
        raise OrchError(
            "sign.convenience_readers",
            f"{what} would add a reader to a workspace whose history has convenience signatures",
            hint="the new reader must understand auth convenience first; orch-core, orch-relay and orch-mobile "
            "releases that know it are needed (orch-v2-host §0.8.6). Upgrade path (owner decision of 13 Oct 2026): "
            "a reader-version handshake is planned for phase 2, with the relay/phone integration; a reader that "
            "declares and proves it understands auth convenience may then be added, older readers stay refused. "
            "Until it exists, use click-to-sign only on workspaces that won't get a phone, relay link or other "
            "member yet",
        )
