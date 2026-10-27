"""After an add-on event lands: bring the instructions in line with the add-on set (design addon-skills D2).

``finish`` calls :func:`after` once the signed events of a request are in the log. When one of them is an add-on grant,
disable or purge, the add-on part of ``orch instructions sync`` runs for the workspace root: ``AGENTS.orch.md`` with the
add-on block and the add-on skills. A failure never undoes the signed event; it is reported as a line of the result
and ``orch instructions sync`` repeats it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

__all__ = ["after", "TYPES"]

TYPES = frozenset({"addon.granted", "addon.disabled", "addon.purged"})
_log = logging.getLogger("orch.host.addonsync")


def refresh(root: Path, store: Any, caps: Any = None) -> list[str]:
    """The add-on part of the sync for ``root`` (and the registered worktrees ``caps`` knows) from the replayed
    ``store``; lines for what failed or was left alone."""
    try:
        from orch import layout as layout_mod
        from orch.instructions import collect_rows, known_worktrees, sync_addon_part

        rows = collect_rows(store.layout.store, store.state.workspace.addons)
        _changed, kept = sync_addon_part(
            root, rows, store=layout_mod.store_of(root), worktrees=known_worktrees(caps) if caps is not None else ()
        )
        return [x for x in kept if x.startswith(("kept", "could not", "skipped"))]
    except Exception as e:  # noqa: BLE001 - the event is signed and in the log; this only refreshes generated files
        _log.warning("instructions sync after an add-on event failed: %s", type(e).__name__)
        return [f"could not refresh the agent instructions ({type(e).__name__}): run orch instructions sync"]
    return []


def after(service: Any, events: list[dict[str, Any]]) -> list[str]:
    """Lines for the result of the request: empty when nothing was to be done or all went well."""
    if not any(e.get("type") in TYPES for e in events):
        return []
    caps = getattr(service, "caps", None)
    return refresh(Path(service.entry.root), service.store, caps)
