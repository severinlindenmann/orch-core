"""The hub's logic for selective-mode shared items (protocol.md §10.2, spec §8a).

* ``put`` / ``revoke`` serve the link ops. They run under the registry lock, so the mode check and the write cannot be
  separated from an unbind; a full-mode (or unbound) workspace gets ``wrong_mode`` / ``not_bound``.
* ``sealed`` is stored exactly as received, never opened here (see :mod:`orch_hub.shares.open`). "Newer" is arrival
  order on the authenticated link (L7 already rejects replays): the last ``share_put`` of an item id wins, identical
  bytes are an idempotent no-op. A real version compare needs a field inside ``sealed`` (reported with D9).
* ``on_unbind`` / ``sync`` delete a workspace's items when its binding ends or leaves selective mode.
* ``list_shared`` is the read API for the web view and the phone. Hub groups and the hub-admin group get no role in a
  selective workspace (spec §8a), so only the hub user the admin aliased as the workspace's owner sees its items.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ..contract.canon import unb64u
from ..contract.hub import Refused
from .store import ShareStore

_log = logging.getLogger("orch_hub.shares")

MAX_ITEM_BYTES = 256 * 1024  # one sealed item
MAX_ITEMS = 1000  # per workspace
MAX_WS_BYTES = 16 * 1024 * 1024  # per workspace


class ShareService:
    def __init__(
        self,
        store: ShareStore,
        registry: Any,
        *,
        now_ms: Callable[[], int] | None = None,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store, self._reg = store, registry
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._on_change = on_change  # optional extra sink for the audit events (the store's share_log always records)

    def _selective(self, ws: str) -> None:
        """The caller holds the registry lock."""
        if self._reg.pin(ws) is None:
            raise Refused("unknown_workspace")
        st = self._reg.binding_state(ws)
        if st is None or st["status"] != "bound":
            raise Refused("not_bound")
        if self._reg.mode(ws) != "selective":
            raise Refused("wrong_mode")

    def _emit(self, ev: dict[str, Any]) -> None:
        if self._on_change is not None:
            try:
                self._on_change(ev)
            except Exception:  # noqa: BLE001 (a failing sink never undoes a stored change; the share_log has it)
                _log.warning("share audit sink failed")

    def put(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        sealed = unb64u(body["sealed"])
        if not sealed:
            raise Refused("malformed")
        if len(sealed) > MAX_ITEM_BYTES:
            raise Refused("too_large")
        item_id = body["item_id"]
        with self._reg.lock, self.store.lock:
            self._selective(ws)
            held = self.store.get(ws, item_id)
            items, total = self.store.usage(ws)
            new_items = items + (0 if held else 1)
            new_total = total - (0 if held is None else len(held[0])) + len(sealed)
            if new_items > MAX_ITEMS or new_total > MAX_WS_BYTES:
                raise Refused("too_large")
            if held is not None and held[0] == sealed:
                return {}  # identical bytes: idempotent
            change = self.store.put(ws, item_id, sealed, self._now())
        self._emit({"change": change, "workspace_id": ws, "item_id": item_id})
        return {}

    def revoke(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        item_id = body["item_id"]
        with self._reg.lock, self.store.lock:
            self._selective(ws)
            gone = self.store.revoke(ws, item_id, self._now())
        if gone:  # revoking an item the hub does not hold is an idempotent {} (a retried revoke)
            self._emit({"change": "revoke", "workspace_id": ws, "item_id": item_id})
        return {}

    def sync(self, ws: str) -> None:
        """Items belong to a live SELECTIVE binding only: any other state deletes them. The caller holds the registry
        lock (binding_put / join) or startup."""
        st = self._reg.binding_state(ws)
        if st is None or st["status"] != "bound" or self._reg.mode(ws) != "selective":
            n = self.store.purge(ws, self._now())
            if n:
                self._emit({"change": "unbind", "workspace_id": ws, "count": n})

    def reconcile(self) -> None:
        """At startup: drop the items of every workspace that is no longer a bound selective one (also after a crash)."""
        with self._reg.lock:
            for ws in self.store.workspaces():
                self.sync(ws)

    # --- read API (D13: the hub web view and the phone) -----------------------------------------------------------

    def list_shared(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """The sealed items of a selective workspace that ``user_id`` may see: the owner alias only. Every other user,
        hub-admins and role holders included, gets ``[]`` (spec §8a). Each entry is ``{item_id, sealed (bytes), digest,
        size, stored_ms}``; opening ``sealed`` is :func:`orch_hub.shares.open_shared_item` (not specified yet)."""
        with self._reg.lock:
            try:
                self._selective(workspace_id)
            except Refused:
                return []
            if type(user_id) is not str or self._reg.owner_alias(workspace_id) != user_id:
                return []
            return self.store.items(workspace_id)
