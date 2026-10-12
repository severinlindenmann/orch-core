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

__all__ = ["has_convenience_events", "refuse_new_reader"]


def has_convenience_events(store: Any) -> bool:
    """True when any event of any log carries ``auth: "convenience"``. Reads every log: only for rare ops."""
    return any(ev.get("auth") == "convenience" for _, ev in store.merged_events())


def refuse_new_reader(store: Any, what: str) -> None:
    if has_convenience_events(store):
        raise OrchError(
            "sign.convenience_readers",
            f"{what} would add a reader to a workspace whose history has convenience signatures",
            hint="the new reader must understand auth convenience first; orch-core, orch-relay and orch-mobile "
            "releases that know it are needed (orch-v2-host §0.8.6)",
        )
