"""Adding a reader to a workspace whose history holds ``auth: "convenience"`` events (orch-v2-host §0.8.6 item 1).

Owner decision, 12 Oct 2026: such a workspace refuses ops that add a reader of its log (``member.add`` now; relay
pairing and device ops when they exist) with ``sign.convenience_readers``, in host and direct mode, until the new
readers understand the value. Switching back to terminal signing does not help: the new reader must still parse the
convenience events already in the history. A workspace with no convenience events yet may switch convenience off and
then add the reader.
"""

from __future__ import annotations

from typing import Any

from orch.ops.errors import OrchError

__all__ = ["READER_OPS", "has_convenience_events", "refuse_new_reader"]

#: ops that add a reader of the workspace's log; the host (:mod:`orch.host.convenience`) and the op handlers use this
#: one set. ``relay.link`` (a relay link) and ``relay.pair`` (the host's ``approved`` answer to a pairing, and any
#: member list that adds a device) are host ops of the relay bridge (orch-v2-host §11A.6)
READER_OPS = frozenset({"member.add", "relay.link", "relay.pair"})


def has_convenience_events(store: Any) -> bool:
    """True when any event of any log carries ``auth: "convenience"``. Reads every log: only for rare ops."""
    return any(ev.get("auth") == "convenience" for _, ev in store.merged_events())


def refuse_new_reader(store: Any, what: str) -> None:
    if has_convenience_events(store):
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
