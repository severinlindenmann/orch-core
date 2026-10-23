"""Startup reconcile (and a periodic retry) of the state that belongs to a live FULL binding only: the stored ``wk_seal``
wraps (D9 review) and the synced ``log_sync`` view (D15 review). The registry is the truth: any workspace that holds
either but is not a bound full one gets its wraps retired and its view dropped. This is the same end state
``link.ops`` reaches on ``hub.unbound`` / a switch to selective; running it again covers a failed step there (those are
logged and go on) and a crash between the binding commit and the step. Idempotent; one failing workspace never stops
the others."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("orch_hub.reconcile")


def reconcile_full_only(registry: Any, wk_seals: Any, view_store: Any) -> None:
    with registry.lock:  # the same lock the link ops hold across a binding change
        for ws in sorted(set(wk_seals.live_workspaces()) | set(view_store.workspaces())):
            if registry.mode(ws) == "full":
                continue
            for what, step in (("wk_seal retire", wk_seals.retire), ("view drop", view_store.drop)):
                try:
                    step(ws)
                except Exception:  # noqa: BLE001 - logged by type only; the next tick retries
                    log.warning("%s of %s failed; retried at the next tick", what, ws[:8])


def reconcile_relay_purge(notifier: Any) -> None:
    """The relay's copy of a revoked channel (#52): channels the store lists as revoked whose purge is not marked done
    are purged at the relay, retried every tick until it succeeds. Covers a revoke made by another process (the CLI),
    whose hook never ran in this one. No relay configured (``notifier`` None): nothing to do."""
    if notifier is not None:
        notifier.purge_pending_async()
