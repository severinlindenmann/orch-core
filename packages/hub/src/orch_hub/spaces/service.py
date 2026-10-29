"""The hub's shared-space logic (protocol.md §10.4), on top of the contract's pure rules (``orch_hub.contract.spaces``).

* Membership changes (create, add, remove, unbind) always make a NEW epoch: a fresh 32-byte ``SK_e`` from the CSPRNG,
  a signed ``hub.space`` (``HK_op``) with its ``sk_commit``, and one signed ``hub.space_wrap`` per member (``SK_e``
  sealed to the member's ``WXK`` from its v2 card). A workspace that leaves gets nothing for later epochs.
* Who may ask for a change is decided here with D3's ``Access``: a hub-admin, or a user with ``admin`` in every
  FULL-mode member workspace. A selective workspace (or one of unknown mode) is added only with its own
  ``space.accept`` (contract ``check_added_members``); the hub-admin can remove it, never add it.
* ``put``, ``revoke``, ``get`` run the contract's ``space_put`` / ``space_revoke`` / ``space_get`` over the stored
  state; the hub opens items it stores (option B).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from ..contract import hub as H
from ..contract import spaces as SP
from ..contract.canon import CanonError, unb64u
from ..contract.hub import Refused
from ..contract.keystore import OpSigner
from .store import SpaceStore

_log = logging.getLogger("orch_hub.spaces")
PAGE_BYTES = 1 << 20  # sealed data per space_get page (protocol.md §10.4)


class SpaceError(Exception):
    """An admin-side refusal with a stable code (the CLI prints it); ``need`` carries what an owner has to sign."""

    def __init__(self, code: str, detail: str = "", need: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.need = code, need


class SpaceService:
    def __init__(
        self,
        store: SpaceStore,
        registry: Any,
        access: Any,
        op_signer: OpSigner,
        hub_id: str,
        op_cert_id: Callable[[], str],
        *,
        now_ms: Callable[[], int] | None = None,
        dev_mode: bool,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store, self._reg, self._acc = store, registry, access
        self._signer, self.hub_id, self._cert = op_signer, hub_id, op_cert_id
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._dev = dev_mode
        self._on_change = on_change  # the audit chain hooks in here (D4)
        self._lock = threading.RLock()
        self._unavailable: set[str] = set()

    # --- admin side: create, add, remove, unbind -----------------------------------------------------------------

    def _authorize(self, actor: str, members: list[str]) -> None:
        """Hub-admin, or ``admin`` in every full-mode member workspace. (A selective member needs its own accept; it
        does not count towards, and cannot be satisfied by, a hub role.)"""
        if self._acc.is_hub_admin(actor):
            return
        full = [w for w in members if self._reg.mode(w) == "full"]
        if not full or any(self._acc.role_of(actor, w) != "admin" for w in full):
            raise SpaceError("forbidden_role", "needs hub-admin, or admin in every full-mode member workspace")

    def _modes(self, members: list[str]) -> dict[str, str | None]:
        return {w: self._reg.mode(w) for w in members}

    def _pins(self, members: list[str]) -> dict[str, bytes]:
        out = {}
        for w in members:
            p = self._reg.pin(w)
            if p is not None:
                out[w] = unb64u(p["wsk_pub"], 65)
        return out

    def _accepts(self, space_id: str, members: list[str]) -> dict[str, Any]:
        return {w: a for w in members if (a := self.store.accept(space_id, w)) is not None}

    def _record(self, space_id: str, epoch: int, members: list[str], sk: bytes) -> dict[str, Any]:
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.space",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "op_cert_id": self._cert(),
            "space_id": space_id,
            "epoch": epoch,
            "members": members,
            "sk_commit": SP.sk_commit(space_id, epoch, sk),
            "issued_ms": self._now(),
            "dev": self._dev,
        }
        return self._signer.sign_object(o)

    def _wrap(self, space_id: str, epoch: int, ws: str, sk: bytes) -> dict[str, Any]:
        # Only the WXK of the VERIFIED v2 card in the registry (delegation by the owner's PK, card by the pinned WSK).
        # Any other shape, or a card that does not verify, is refused: a wrap is never sealed to an unverified key.
        pin = self._reg.pin(ws)
        try:
            if pin is None:
                raise Refused("unknown_workspace")
            c = H.verify_v2_card(self._reg.card(ws), unb64u(pin["pk_pub"], 65), now_ms=self._now())
            if c["workspace_id"] != ws or c["wsk_pub"] != pin["wsk_pub"]:
                raise Refused("other_workspace")
            wxk_pub, ver = unb64u(c["wxk_pub"], 65), int(c["wxk_version"])
        except Refused, TypeError, KeyError, CanonError, ValueError:
            raise SpaceError("unknown_workspace", "no verified v2 card (wxk) for a member") from None
        sealed = SP.seal_sk(
            sk,
            hub_id=self.hub_id,
            space_id=space_id,
            epoch=epoch,
            workspace_id=ws,
            wxk_pub=wxk_pub,
            wxk_version=ver,
            eph_seed=os.urandom(32),
        )
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.space_wrap",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "op_cert_id": self._cert(),
            "space_id": space_id,
            "epoch": epoch,
            "workspace_id": ws,
            "wxk_version": ver,
            "sealed": sealed,
            "dev": self._dev,
        }
        return self._signer.sign_object(o)

    def _new_epoch(
        self,
        space_id: str,
        epoch: int,
        old: list[str] | None,
        members: list[str],
        *,
        change: str,
        workspace_id: str | None,
        actor: str | None,
        name: str | None = None,
    ) -> dict[str, Any]:
        members = sorted(set(members))
        sk = os.urandom(32)
        rec = self._record(space_id, epoch, members, sk)
        if old is not None or name is not None:
            try:
                SP.check_added_members(
                    old,
                    rec["o"],
                    modes=self._modes(members),
                    accepts=self._accepts(space_id, members),
                    wsk_pubs=self._pins(members),
                    dev_mode=self._dev,
                )
            except Refused as e:
                if e.code == "not_accepted":
                    added = [w for w in members if old is None or w not in old]
                    raise SpaceError(
                        "not_accepted",
                        "a selective workspace is added only with its owner's space.accept",
                        need={
                            "space_id": space_id,
                            "epoch": epoch,
                            "prev_members_hash": SP.members_hash(sorted(old or [])),
                            "members_hash": SP.members_hash(members),
                            "workspaces": [w for w in added if self._reg.mode(w) != "full"],
                        },
                    ) from None
                raise SpaceError(e.code) from None
        wraps = {w: self._wrap(space_id, epoch, w, sk) for w in members}
        # audited BEFORE it commits: a failed commit leaves a record for a change that did not happen, never the reverse
        self._changed(
            {
                "space_id": space_id,
                "epoch": epoch,
                "change": change,
                "workspace_id": workspace_id,
                "actor_user_id": actor,
            }
        )
        self.store.add_epoch(space_id, epoch, members, sk, rec, wraps, name=name)
        return rec["o"]

    def _changed(self, ev: dict[str, Any]) -> None:
        if self._on_change is not None:
            self._on_change(ev)

    def create(
        self, name: str, members: list[str], actor: str, kind: str | None = None, purpose: str = ""
    ) -> dict[str, Any]:
        if type(name) is not str or not name or len(name) > 64 or not name.isprintable():
            raise SpaceError("malformed", "name")
        if not members or len(members) > SP.MAX_MEMBERS or any(not H._hex16(w) for w in members):
            raise SpaceError("malformed", "members")
        with self._lock:
            for w in members:
                if self._reg.mode(w) is None:
                    raise SpaceError("unknown_workspace", w)
            self._authorize(actor, members)
            space_id = os.urandom(16).hex()
            rec = self._new_epoch(
                space_id, 1, None, members, name=name, change="create", workspace_id=None, actor=actor
            )
            if kind is not None:  # what the console's dialog said the space is for (not signed, not secret)
                self.store.set_meta(space_id, kind, purpose)
            return rec

    @staticmethod
    def _sid(space_id: Any) -> str:
        if not H._hex16(space_id):
            raise SpaceError("malformed", "space id")
        return space_id

    def _current(self, space_id: str) -> tuple[int, list[str]]:
        self._sid(space_id)
        e = self.store.current_epoch(space_id)
        row = None if e is None else self.store.epoch_row(space_id, e)
        if row is None:
            raise SpaceError("unknown_space", space_id)
        return e, row[0]

    def add(self, space_id: str, ws: str, actor: str) -> dict[str, Any]:
        with self._lock:
            e, members = self._current(space_id)
            if self._reg.mode(ws) is None:
                raise SpaceError("unknown_workspace", ws)
            if ws in members:
                raise SpaceError("already_member", ws)
            self._authorize(actor, [*members, ws])
            return self._new_epoch(space_id, e + 1, members, [*members, ws], change="add", workspace_id=ws, actor=actor)

    def remove(self, space_id: str, ws: str, actor: str | None) -> dict[str, Any] | None:
        """Remove a member (the hub-admin may remove any, including a selective one). ``actor`` None = the hub itself
        (unbind). Returns the new record, or None when the last member left and the space ended."""
        with self._lock:
            e, members = self._current(space_id)
            if ws not in members:
                raise SpaceError("not_member", ws)
            if actor is not None:
                self._authorize(actor, members)
            rest = [m for m in members if m != ws]
            change = "remove" if actor is not None else "unbind"
            if not rest:
                self._changed(
                    {"space_id": space_id, "epoch": e, "change": "delete", "workspace_id": ws, "actor_user_id": actor}
                )
                self.store.delete_space(space_id)
                return None
            rec = self._new_epoch(space_id, e + 1, members, rest, change=change, workspace_id=ws, actor=actor)
            self.store.drop_accept(space_id, ws)  # only after the audit record and the new epoch are in
            self.store.drop_mode(space_id, ws)  # and its member mode: a return starts from the default
            return rec

    def on_unbind(self, ws: str) -> None:
        """``hub.unbound`` of ``ws`` was accepted: it leaves every space it is in, one new epoch each, in the same
        step (protocol.md §10.4). A space that cannot be rotated now is marked unavailable and retried (never raises
        into the link: the unbind is already recorded)."""
        self.on_mode_change(ws)

    def on_mode_change(self, ws: str) -> None:
        """``ws`` left the hub or went full -> selective: it leaves every space it is in (rotation), exactly like an
        unbind. It comes back only through the add path, i.e. with its owner's ``space.accept`` for that transition."""
        with self._lock:
            for sid in self.store.spaces_of(ws):
                self._reconcile_space(sid)

    def _stale_member(self, sid: str, w: str) -> bool:
        """A member that must not be one: no longer bound, or selective without the accept that put it there."""
        m = self._reg.mode(w)
        return m is None or (m != "full" and self.store.accept(sid, w) is None)

    def _reconcile_space(self, sid: str) -> bool:
        """Remove every stale member of one space. On failure the space is marked unavailable (its ops refuse with
        ``unavailable``) until a later attempt succeeds. Returns whether the space is consistent."""
        try:
            while True:
                if not self.store.exists(sid):
                    break
                _, members = self._current(sid)
                stale = [w for w in members if self._stale_member(sid, w)]
                if not stale:
                    break
                self.remove(sid, stale[0], None)
        except Exception:  # noqa: BLE001  # logged without detail; the space stays closed
            _log.exception("space %s: could not rotate out a departed member", sid)
            self._unavailable.add(sid)
            return False
        self._unavailable.discard(sid)
        return True

    def reconcile(self) -> None:
        """Start-up (and retry) pass over all spaces. Never raises: the hub starts; a space that cannot be made
        consistent refuses ops until a later pass does."""
        with self._lock:
            for sid in self.store.space_ids():
                self._reconcile_space(sid)

    def _gate_id(self, space_id: Any) -> None:
        if not H._hex16(space_id):
            raise Refused("malformed")
        self._gate(space_id)

    def _gate(self, space_id: str) -> None:
        """Ops on a space that is waiting for reconciliation retry it once and otherwise refuse."""
        if space_id in self._unavailable and not self._reconcile_space(space_id):
            raise Refused("unavailable")

    def set_member_mode(self, space_id: str, ws: str, mode: str, actor: str) -> str:
        """Admin: ``reads`` (the hub refuses this member's puts and revokes at once) or ``edits``. Same authority as changing
        the member list; it changes no signed record and no key."""
        if mode not in ("reads", "edits"):
            raise SpaceError("malformed", "mode")
        with self._lock:
            _e, members = self._current(self._sid(space_id))
            if ws not in members:
                raise SpaceError("not_member", ws)
            self._authorize(actor, members)
            self.store.set_mode(space_id, ws, mode)
            return mode

    def set_archived(self, space_id: str, archived: bool, actor: str, now_ms: int) -> bool:
        """Admin: an archived space refuses new puts and revokes and is hidden from the default list; reads go on."""
        with self._lock:
            _e, members = self._current(self._sid(space_id))
            self._authorize(actor, members)
            self.store.set_archived(space_id, now_ms if archived else None)
            return archived

    def list_all(self) -> list[dict[str, Any]]:
        out = []
        for sid in self.store.space_ids():
            e, members = self._current(sid)
            items = self.store.items(sid)
            out.append(
                {
                    "space_id": sid,
                    "name": self.store.name(sid),
                    "epoch": e,
                    "members": members,
                    "items": sum(1 for v in items.values() if not v["revoked"]),
                    "revoked": sum(1 for v in items.values() if v["revoked"]),
                    "modes": {w: self.store.mode(sid, w) for w in members},
                    "archived_ms": self.store.archived_ms(sid),
                }
            )
        return out

    def read_items(self, space_id: str, limit: int = 300) -> list[dict[str, Any]]:
        """Admin read (option B: the hub holds every ``SK_e`` and may open what it stores). Newest first, at most ``limit``
        rows. Each row: ``item_id``, ``seq``, ``ws`` (the author as the hub recorded it), ``revoked`` and ``item`` (the
        verified inner ``space.item`` object, or None when revoked or when it does not verify against the pins). The
        hub signs nothing here and changes nothing."""
        with self._lock:
            self._current(self._sid(space_id))
            rows = sorted(self.store.items(space_id).items(), key=lambda kv: -kv[1]["seq"])[:limit]
            epochs: dict[int, tuple[list[str], bytes] | None] = {}
            out = []
            for item_id, r in rows:
                item = None
                if not r["revoked"]:
                    if r["epoch"] not in epochs:
                        row = self.store.epoch_row(space_id, r["epoch"])
                        epochs[r["epoch"]] = None if row is None else (row[0], row[1])
                    ep, sealed = epochs[r["epoch"]], self.store.sealed(space_id, item_id)
                    if ep is not None and sealed:
                        try:
                            item = SP.verify_item(
                                sealed,
                                sk=ep[1],
                                space_id=space_id,
                                epoch=r["epoch"],
                                item_id=item_id,
                                members=ep[0],
                                wsk_pubs=self._pins(ep[0]),
                                hub_id=self.hub_id,
                                dev_mode=self._dev,
                            )
                        except Refused:
                            item = None
                out.append({"item_id": item_id, "seq": r["seq"], "ws": r["ws"], "revoked": r["revoked"], "item": item})
            return out

    # --- link side: the ops of §10.4 -----------------------------------------------------------------------------

    def _state(self, space_id: str) -> dict[str, Any] | None:
        if not H._hex16(space_id):
            raise Refused("malformed")  # before any lookup
        e = self.store.current_epoch(space_id)
        row = None if e is None else self.store.epoch_row(space_id, e)
        if row is None:
            return None
        return {
            "space": row[2]["o"],
            "history": self.store.history(space_id),
            "items": self.store.items(space_id),
            "next_seq": self.store.next_seq(space_id),
        }

    def list_for(self, ws: str) -> list[dict[str, Any]]:
        return [
            self.store.epoch_row(sid, self.store.current_epoch(sid))[2]  # type: ignore[index]
            for sid in self.store.spaces_of(ws)
            if sid not in self._unavailable
        ]

    def accept(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        """Store a selective workspace's ``space.accept`` (checked again when it is used)."""
        signed = body["accept"]
        o = H._envelope(signed, "space.accept")
        pin = self._reg.pin(ws)
        if pin is None:
            raise Refused("unknown_workspace")
        H._signature(signed, H.L["sig_space_accept"], unb64u(pin["wsk_pub"], 65))
        H._fields(o, "space.accept")
        H._dev(o, self._dev)
        if o["workspace_id"] != ws:
            raise Refused("other_workspace")
        if o["hub_id"] != self.hub_id:
            raise Refused("other_hub")
        self.store.put_accept(o["space_id"], ws, signed)
        return {}

    def _writable(self, space_id: str, ws: str) -> None:
        """The admin's policy for writes: an archived space and a ``reads`` member refuse (the signed rules stay as they
        were; this only restricts). Membership comes first: a workspace that is not in the space is told ``not_member`` and
        learns nothing about whether the space is archived."""
        st = self._state(space_id)
        if st is not None and ws not in st["space"]["members"]:
            raise Refused("not_member")
        if self.store.archived_ms(space_id) is not None:
            raise Refused("archived")
        if self.store.mode(space_id, ws) == "reads":
            raise Refused("read_only")

    def put(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._gate_id(body["space_id"])
            self._writable(body["space_id"], ws)
            st = self._state(body["space_id"])
            sks = {}
            if st is not None:
                row = self.store.epoch_row(body["space_id"], st["space"]["epoch"])
                sks = {st["space"]["epoch"]: row[1]}  # type: ignore[index]
            members = [] if st is None else st["space"]["members"]
            new, reply = SP.space_put(st, ws, body, sks=sks, wsk_pubs=self._pins(members), dev_mode=self._dev)
            if body["item_id"] not in (st or {}).get("items", {}):  # a new item (the same put again is idempotent)
                row = new["items"][body["item_id"]]
                self.store.put_item(body["space_id"], body["item_id"], row, body["sealed"], new["next_seq"])
            return reply

    def revoke(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._gate_id(body["space_id"])
            self._writable(body["space_id"], ws)
            st = self._state(body["space_id"])
            new, reply = SP.space_revoke(st, ws, body)
            if st is not None and new["next_seq"] != st["next_seq"]:
                self.store.revoke_item(body["space_id"], body["item_id"], reply["seq"], new["next_seq"])
            return reply

    def get(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            sid = body["space_id"]
            self._gate_id(sid)
            st = self._state(sid)
            r = SP.space_get(st, ws, body)
            if st is None:  # unreachable: space_get raised not_member
                raise Refused("not_member")
            entries, size = [], 0
            for e in r["entries"]:
                sealed = None if e["revoked"] else self.store.sealed(sid, e["item_id"])
                if sealed is not None:
                    if entries and size + len(sealed) > PAGE_BYTES:
                        break
                    size += len(sealed)
                entries.append({**e, **({} if sealed is None else {"sealed": sealed})})
            epochs = sorted({st["space"]["epoch"], *(e["epoch"] for e in entries)})
            wraps = [w for ep in epochs if (w := self.store.wrap(sid, ep, ws)) is not None]
            records = [
                row[2]
                for ep in epochs
                if ep != st["space"]["epoch"] and (row := self.store.epoch_row(sid, ep)) is not None
            ]
            return {
                "space": self.store.epoch_row(sid, st["space"]["epoch"])[2],  # type: ignore[index]
                "wraps": wraps,
                "records": records,
                "entries": entries,
                "next": entries[-1]["seq"] if entries else body["since"],
            }
