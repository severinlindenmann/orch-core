"""The hub's logic for shared items of a bound workspace (selective or full) (protocol.md §10.2, spec §8a).

* ``put`` / ``revoke`` serve the link ops. They run under the registry lock, so the mode check and the write cannot be
  separated from an unbind; an unbound workspace gets ``not_bound``; in full mode only ticket items are accepted (``wrong_mode`` otherwise).
* ``share_put`` follows protocol.md §10.2b: the hub opens ``sealed`` (:mod:`orch_hub.shares.open`), checks the WSK
  signature and content, and stores the SEALED bytes. A higher ``item_version`` replaces, the same version with the same
  bytes is an idempotent ``{}``, an equal or lower one (or a revoked one) is ``stale_version``. ``share_revoke`` is
  idempotent and keeps a tombstone. Each accepted put, revoke or purge is a ``share`` event on the hub audit chain, written
  BEFORE the change commits.
* ``on_unbind`` / ``sync`` delete a workspace's items when its binding ends.
* ``list_shared`` is the read API for the web view and the phone. Hub groups and the hub-admin group get no role in a
  selective workspace (spec §8a): the hub user the admin aliased as the workspace's owner sees every item; anyone else
  sees only the TICKET items whose owner-signed ``audience`` names them (by id) or a group they belong to now (P02,
  contract 10.2b). The audience is the owner's word, signed by ``WSK``; no hub role or admin can add a reader, and no
  reader can sign (``shared_item`` stays owner-only).
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
        dev_mode: bool,
        now_ms: Callable[[], int] | None = None,
        on_change: Callable[[dict[str, Any]], Any] | None = None,
        access: Any = None,
    ) -> None:
        """``hxk`` is the hub's key-agreement key (an EC private key, or :func:`orch_hub.shares.hxk_handle`).
        ``on_change`` receives the chain's ``share`` event of every accepted put, revoke and purge (before it commits)."""
        self.store, self._reg = store, registry
        self._hub, self._hxk, self._dev = hub_id, hxk, dev_mode
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._on_change = on_change
        self._access = access  # the hub's Access (P02): resolves a signed audience; None: owner alias only
        self._pending: set[str] = (
            set()
        )  # workspaces whose purge failed (audit down): retried at the next sync/reconcile

    def _selective(self, ws: str) -> None:
        """Bound and selective (custodial build only). The caller holds the registry lock."""
        self._bound(ws)
        if self._reg.mode(ws) != "selective":
            raise Refused("wrong_mode")

    def _bound(self, ws: str) -> None:
        """Bound, in selective or full mode. The caller holds the registry lock."""
        if self._reg.pin(ws) is None:
            raise Refused("unknown_workspace")
        if self._is_pending(ws):
            self._retry(ws)  # D9-1: heal here when the sink is back; a rebound workspace must not stay refused
            if self._is_pending(ws):
                raise Refused("not_bound")  # stale items of an earlier binding are still there: fail closed
        st = self._reg.binding_state(ws)
        if st is None or st["status"] != "bound":
            raise Refused("not_bound")
        if self._reg.mode(ws) not in ("selective", "full"):
            raise Refused("wrong_mode")

    def _is_pending(self, ws: str) -> bool:
        return ws in self._pending or ws in self.store.pending()

    def _mark(self, ws: str) -> None:
        """Remember that ``ws`` has stale items. The in-memory mark cannot fail, so it holds even when the durable one
        does (D9-2): the workspace is then refused until a purge succeeds or, after a restart, ``reconcile``."""
        self._pending.add(ws)
        try:
            self.store.mark_pending(ws)
        except Exception:  # noqa: BLE001
            _log.exception("could not persist the share_pending mark; held in memory")

    def _clear(self, ws: str) -> None:
        self.store.clear_pending(ws)
        self._pending.discard(ws)

    def _retry(self, ws: str) -> None:
        try:
            self.purge(ws)
            self._clear(ws)
        except Exception:  # noqa: BLE001  still failing (sink or store down): stays pending
            _log.warning("share purge retry failed; still pending")

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
            self._bound(ws)
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
            if self._reg.mode(ws) == "full" and r["item"]["item_kind"] != "ticket":
                raise Refused("wrong_mode")  # full mode: ticket items only (the hub's views already cover the rest)
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
            self._bound(ws)
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
        """Items belong to a live bound workspace (selective or full) only: any other state deletes them. The caller holds
        the registry lock (binding_put / join) or startup. Never raises: a failing purge (audit sink down) keeps the items, marks the
        workspace for a retry (here, at the next sync or reconcile) and lets the other unbind steps go on; until the
        retry succeeds the workspace serves and accepts nothing."""
        try:
            if self._is_pending(ws):
                self.purge(ws)  # the items are those of an earlier binding, whatever the state is now
                self._clear(ws)
            st = self._reg.binding_state(ws)
            if st is None or st["status"] != "bound":
                self.purge(ws)
        except Exception:  # noqa: BLE001  any sink failure must leave the other unbind steps running
            self._mark(ws)
            _log.exception("share purge failed; items kept, retry at the next sync")

    def reconcile(self) -> None:
        """At startup: drop the items of every workspace that is no longer a bound one (also after a crash).
        A failure of one workspace is logged and retried later; it never stops the startup."""
        with self._reg.lock:
            for ws in sorted(set(self.store.workspaces()) | self.store.pending() | self._pending):
                self.sync(ws)

    def retry_pending(self) -> None:
        """Timer tick (D9-1): retry the purge of every workspace still pending, so stale items do not outlive the sink."""
        with self._reg.lock:
            for ws in sorted(self._pending | self.store.pending()):
                self.sync(ws)

    # --- read API (D13: the hub web view and the phone) -----------------------------------------------------------

    def list_shared(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """The shared items of a selective workspace that ``user_id`` may see. The owner alias sees all of them. Anyone
        else sees only ticket items whose verified, owner-signed ``audience`` names them or a group they are a member of
        now (and only while they are an active hub user); hub-admins and role holders get nothing by their role
        (spec §8a). Each entry is ``{item_id, sealed (bytes), digest, size, stored_ms, item_version, item (the verified
        share.item), signed (the signed item), hash}``; an item that no longer opens and verifies (bad wrap, signature
        or content) is left out (fail closed)."""
        with self._reg.lock:
            try:
                self._bound(workspace_id)
            except Refused:
                return []
            if type(user_id) is not str or not user_id:
                return []
            rows = self._open_all(workspace_id, self.store.items(workspace_id))
            if self._reg.mode(workspace_id) == "full":  # a leftover summary from before a switch is never served
                rows = [it for it in rows if it["item"].get("item_kind") == "ticket"]
            if self._reg.owner_alias(workspace_id) == user_id:
                return rows
            return [it for it in rows if self.audience_reads(user_id, it["item"])]

    def audience_reads(self, user_id: str, item: dict[str, Any]) -> bool:
        """May ``user_id`` read this VERIFIED item through its signed audience (not as the owner)? Fail closed on every
        doubt: no Access, no audience, not a ticket item, an inactive or unknown user, any lookup error."""
        aud = item.get("audience")
        if self._access is None or type(aud) is not dict or item.get("item_kind") != "ticket":
            return False
        try:
            if not self._access.is_active(user_id):
                return False
            if user_id in aud["users"]:
                return True
            return any(user_id in self._access.members(g) for g in aud["groups"])
        except Exception:  # noqa: BLE001  (a malformed id, a closed store, ...): nothing is read
            return False

    def shared_item(self, user_id: str, workspace_id: str, item_id: str) -> dict[str, Any]:
        """One live shared item for the custodial build (protocol.md §8.6): the same entry as :meth:`list_shared`.
        ``not_granted`` for anyone but the owner alias (and for a workspace that is not a bound selective one),
        ``unknown_item`` when the item is not held, revoked, or no longer opens and verifies."""
        with self._reg.lock:
            try:
                self._selective(workspace_id)
            except Refused:
                raise Refused("not_granted") from None
            if type(user_id) is not str or self._reg.owner_alias(workspace_id) != user_id:
                raise Refused("not_granted")
            rows = [it for it in self.store.items(workspace_id) if it["item_id"] == item_id]
            opened = self._open_all(workspace_id, rows)
            if not opened:
                raise Refused("unknown_item")
            return opened[0]

    def _open_all(self, workspace_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wsk = self._wsk(workspace_id)
        out = []
        for it in rows:
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
            out.append({**it, "item": o["item"], "signed": o["signed"], "hash": o["hash"]})
        return out
