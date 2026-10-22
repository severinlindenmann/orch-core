"""The hub's logic for selective-mode shared items (protocol.md §10.2, spec §8a).

* ``put`` / ``revoke`` serve the link ops. They run under the registry lock, so the mode check and the write cannot be
  separated from an unbind; a full-mode (or unbound) workspace gets ``wrong_mode`` / ``not_bound``.
* ``share_put`` follows protocol.md §10.2b: the hub opens ``sealed`` (:mod:`orch_hub.shares.open`), checks the WSK
  signature and content, and stores the SEALED bytes. A higher ``item_version`` replaces, the same version with the same
  bytes is an idempotent ``{}``, an equal or lower one (or a revoked one) is ``stale_version``. ``share_revoke`` is
  idempotent and keeps a tombstone. Each accepted put, revoke or purge is a ``share`` event on the hub audit chain, written
  BEFORE the change commits.
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
from .open import check_put, open_shared_item
from .store import ShareStore

_log = logging.getLogger("orch_hub.shares")


class ShareService:
    def __init__(
        self,
        store: ShareStore,
        registry: Any,
        *,
        hub_id: str,
        hxk: Any,
        dev_mode: bool = True,
        now_ms: Callable[[], int] | None = None,
        on_change: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """``hxk`` is the hub's key-agreement key (an EC private key, or :func:`orch_hub.shares.hxk_handle`).
        ``on_change`` receives the chain's ``share`` event of every accepted put, revoke and purge (before it commits)."""
        self.store, self._reg = store, registry
        self._hub, self._hxk, self._dev = hub_id, hxk, dev_mode
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._on_change = on_change

    def _selective(self, ws: str) -> None:
        """The caller holds the registry lock."""
        if self._reg.pin(ws) is None:
            raise Refused("unknown_workspace")
        st = self._reg.binding_state(ws)
        if st is None or st["status"] != "bound":
            raise Refused("not_bound")
        if self._reg.mode(ws) != "selective":
            raise Refused("wrong_mode")

    def _wsk(self, ws: str) -> bytes:
        pin = self._reg.pin(ws)
        if pin is None:
            raise Refused("unknown_workspace")
        return unb64u(pin["wsk_pub"], 65)

    def _emit(self, ev: dict[str, Any]) -> None:
        if self._on_change is not None:
            self._on_change(ev)  # a failing audit sink refuses the change (fail closed): the chain is the record

    def put(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._reg.lock, self.store.lock:
            self._selective(ws)
            item_id = body["item_id"]
            held = self.store.held(ws, item_id)
            live, _ = self.store.usage(ws)
            r = check_put(
                body,
                hub_id=self._hub,
                workspace_id=ws,
                hxk=self._hxk,
                wsk_pub=self._wsk(ws),
                dev_mode=self._dev,
                held=held,
                live_items=live,
            )
            if not r["new"]:
                return {}  # the same version with the same bytes: a resend
            version = r["item"]["item_version"]
            self._emit(
                {
                    "change": "put",
                    "workspace_id": ws,
                    "item_id": item_id,
                    "item_version": version,
                    "item_hash": r["hash"],
                }
            )
            self.store.put(ws, item_id, unb64u(body["sealed"]), version, r["hash"], self._now())
        return {}

    def revoke(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        item_id = body["item_id"]
        with self._reg.lock, self.store.lock:
            self._selective(ws)
            held = self.store.held(ws, item_id)
            if held is None or held["revoked"]:
                return {}  # unknown or already revoked: an idempotent {} (a retried revoke)
            self._emit(
                {
                    "change": "revoke",
                    "workspace_id": ws,
                    "item_id": item_id,
                    "item_version": held["item_version"],
                    "item_hash": None,
                }
            )
            self.store.revoke(ws, item_id, self._now())
        return {}

    def purge(self, ws: str) -> None:
        """Delete every live item of ``ws`` (unbind, switch to full). One ``purge`` record on the chain with the count,
        written BEFORE the delete commits (fail closed: a failing sink leaves the items in place). Nothing live: no
        record. The caller holds the registry lock."""
        with self.store.lock:
            n, _ = self.store.usage(ws)
            if n == 0:
                return
            self._emit(
                {
                    "change": "purge",
                    "workspace_id": ws,
                    "item_id": None,
                    "item_version": None,
                    "item_hash": None,
                    "count": n,
                }
            )
            self.store.purge(ws, self._now())

    def sync(self, ws: str) -> None:
        """Items belong to a live SELECTIVE binding only: any other state deletes them. The caller holds the registry
        lock (binding_put / join) or startup."""
        st = self._reg.binding_state(ws)
        if st is None or st["status"] != "bound" or self._reg.mode(ws) != "selective":
            self.purge(ws)

    def reconcile(self) -> None:
        """At startup: drop the items of every workspace that is no longer a bound selective one (also after a crash)."""
        with self._reg.lock:
            for ws in self.store.workspaces():
                self.sync(ws)

    # --- read API (D13: the hub web view and the phone) -----------------------------------------------------------

    def list_shared(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """The shared items of a selective workspace that ``user_id`` may see: the owner alias only. Every other user,
        hub-admins and role holders included, gets ``[]`` (spec §8a). Each entry is ``{item_id, sealed (bytes), digest,
        size, stored_ms, item_version, item (the verified share.item), hash}``; an item that no longer opens and
        verifies (bad wrap, signature or content) is left out (fail closed)."""
        with self._reg.lock:
            try:
                self._selective(workspace_id)
            except Refused:
                return []
            if type(user_id) is not str or self._reg.owner_alias(workspace_id) != user_id:
                return []
            wsk = self._wsk(workspace_id)
            out = []
            for it in self.store.items(workspace_id):
                try:
                    o = open_shared_item(
                        it["sealed"],
                        hub_id=self._hub,
                        workspace_id=workspace_id,
                        item_id=it["item_id"],
                        hxk=self._hxk,
                        wsk_pub=wsk,
                        dev_mode=self._dev,
                    )
                except Refused:
                    _log.warning("a stored shared item no longer verifies; left out")
                    continue
                out.append({**it, "item": o["item"], "hash": o["hash"]})
            return out
