"""The hub audit chain (protocol.md §10.5, D4): one hash-chained, ``HK_op``-signed record per custodial signature, grant
or membership change and space change, with blinded scope tags. Acks are recorded, never enforced (SHORTCUT L7)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from ..contract import audit as AU
from ..contract import hub as H
from ..contract.canon import b64u
from ..contract.hub import Refused
from ..contract.keystore import OpSigner
from .store import AuditStore

#: ``actor_user_id`` of a change made at the hub's own terminal (the dev CLI) rather than by a named hub user.
OPERATOR = "0" * 32
PAGE = 100


class AuditLog:
    def __init__(
        self,
        store: AuditStore,
        op_signer: OpSigner,
        hub_id: str,
        op_cert_id: Callable[[], str],
        *,
        op_cert_by_id: Callable[[str], dict[str, Any] | None] | None = None,
        now_ms: Callable[[], int] | None = None,
        dev_mode: bool,
    ) -> None:
        self.store, self._signer, self.hub_id, self._cert = store, op_signer, hub_id, op_cert_id
        self._certs = op_cert_by_id or (lambda _i: None)
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._dev = dev_mode
        self._lock = threading.Lock()

    # --- appending -----------------------------------------------------------------------------------------------

    def append(self, typ: str, event: dict[str, Any]) -> dict[str, Any]:
        """Add one record. The event is checked with the contract's own field rules (its ``nonce`` is drawn here);
        the record is signed under the current op cert and stored in order. Returns the signed record."""
        ev = dict(event)
        if "nonce" not in ev:  # one rule, no type exempt: 32 random bytes inside every event (protocol.md §10.5)
            ev["nonce"] = b64u(os.urandom(32))
        AU.check_event(typ, ev)
        with self._lock:
            head = self.store.head()
            seq = 1 if head is None else head["seq"] + 1
            o = {
                "v": 2,
                "suite": 2,
                "kind": "hub.audit",
                "hv": H.HV,
                "hub_id": self.hub_id,
                "op_cert_id": self._cert(),
                "seq": seq,
                "prev": AU.GENESIS_PREV if head is None else head["hash"],
                "ts_ms": max(self._now(), 0 if head is None else head["ts_ms"]),  # never decreases along the chain
                "type": typ,
                "scope_tag": AU.scope_tag_of(ev),
                "event_hash": AU.event_hash(ev),
                "dev": self._dev,
            }
            signed = self._signer.sign_object(o)
            self.store.append(seq, AU.chain_hash(signed), o["ts_ms"], signed, ev)
            return signed

    # the producers: one method per kind of change, so call sites cannot build events by hand ------------------------

    def custodial(
        self, *, workspace_id: str, log: str, verb: str, user_id: str, channel_id: str, digest: str
    ) -> dict[str, Any]:
        """Every custodial signature (phase 4; no caller yet)."""
        return self.append(
            "custodial",
            {
                "workspace_id": workspace_id,
                "log": log,
                "verb": verb,
                "user_id": user_id,
                "channel_id": channel_id,
                "digest": digest,
            },
        )

    def access_change(self, ev: dict[str, Any]) -> dict[str, Any]:
        """The observer of D3's ``Access`` (grant and membership changes)."""
        actor = ev.get("actor_user_id") or OPERATOR
        if ev["kind"] == "market_policy":
            return self.append(
                "market_policy",
                {"version": ev["version"], "policy_hash": ev["policy_hash"], "actor_user_id": ev["actor_user_id"]},
            )
        if ev["kind"] == "grant":
            return self.append(
                "grant",
                {
                    "group": ev["group"],
                    "workspace_id": ev["workspace_id"],
                    "role": ev["role"],
                    "actor_user_id": actor,
                },
            )
        return self.append(
            "membership",
            {"group": ev["group"], "user_id": ev["user_id"], "added": ev["added"], "actor_user_id": actor},
        )

    def space_change(self, ev: dict[str, Any]) -> dict[str, Any]:
        """The ``on_change`` hook of the space service."""
        return self.append(
            "space",
            {
                "space_id": ev["space_id"],
                "epoch": ev["epoch"],
                "change": ev["change"],
                "workspace_id": ev.get("workspace_id"),
                "actor_user_id": ev.get("actor_user_id"),
            },
        )

    def share_change(self, ev: dict[str, Any]) -> dict[str, Any]:
        """The ``on_change`` hook of the share service (§10.2b, §10.5): ``put``, ``revoke`` or ``purge``."""
        return self.append(
            "share",
            {
                "workspace_id": ev["workspace_id"],
                "item_id": ev.get("item_id"),
                "change": ev["change"],
                "item_version": ev.get("item_version"),
                "item_hash": ev.get("item_hash"),
                "count": ev.get("count"),
            },
        )

    def channel_change(
        self, *, channel_id: str, user_id: str, change: str, factor: str, actor_user_id: str | None
    ) -> dict[str, Any]:
        """A channel was registered or revoked (§10.5; revocation is permanent). ``actor_user_id`` None = the operator."""
        return self.append(
            "channel",
            {
                "channel_id": channel_id,
                "user_id": user_id,
                "change": change,
                "factor": factor,
                "actor_user_id": actor_user_id,
            },
        )

    def hub_settings(self, *, change: str, actor_user_id: str) -> dict[str, Any]:
        """An admin changed the hub's name/company (``general``) or its advertised addresses (``addresses``): which one and
        who, never the value (M4, #1461). Written before the change is saved; a failure here refuses the change."""
        return self.append("hub_settings", {"change": change, "actor_user_id": actor_user_id})

    def hub_backup(self, *, change: str, actor_user_id: str | None) -> dict[str, Any]:
        """The hub backup key was set/replaced (``key_set``; ``key_confirmed`` once the person saved the new words), a backup was made (``made``) or handed out (``downloaded``):
        which one and who (None = the schedule or the hub terminal), never a key, a word or a file (M4 S5, cond 23)."""
        return self.append("hub_backup", {"change": change, "actor_user_id": actor_user_id})

    # --- the link ops --------------------------------------------------------------------------------------------

    def get(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        """``audit_get``: up to :data:`PAGE` records after ``since``; the event only when it concerns the caller (its
        blinded tag is of this workspace), the hashes of all others stay in the records; the head; the root-signed op
        certs the page names."""
        if body["since"] >= 1 << 63:  # beyond what the store can hold: a malformed request, not an internal error
            raise Refused("malformed")
        rows = self.store.after(body["since"], PAGE)
        records, certs, seen = [], [], set()
        for _seq, signed, ev in rows:
            item: dict[str, Any] = {"record": signed}
            if ev.get("workspace_id") == ws:
                item["event"] = ev
            records.append(item)
            cid = signed["o"]["op_cert_id"]
            if cid not in seen:
                seen.add(cid)
                if (c := self._certs(cid)) is not None:
                    certs.append(c)
        head = self.store.head()
        return {
            "records": records,
            "head": None if head is None else {"seq": head["seq"], "hash": head["hash"]},
            "op_certs": certs,
        }

    def ack(self, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        """``audit_ack``: the head must be the hash of the hub's own record ``seq`` (``audit_fork`` otherwise); acks
        only rise. Recorded, never enforced (L7)."""
        chain = _Chain(self.store)
        res = AU.check_ack(chain, {ws: self.store.acked(ws)}, ws, body)  # type: ignore[arg-type]
        self.store.put_ack(ws, res["acked"])
        return res


class _Chain:
    """The stored chain as the contract's ``check_ack`` wants it: ``len`` and the record at ``seq - 1``."""

    def __init__(self, store: AuditStore) -> None:
        self._s = store

    def __len__(self) -> int:
        return self._s.length()

    def __getitem__(self, i: int) -> dict[str, Any]:
        rows = self._s.after(i, 1)
        if not rows or rows[0][0] != i + 1:
            raise Refused("audit_fork")
        return rows[0][1]


AUDIT_FILE = "audit.db"


def open_log(state: Any, keys: Any, *, dev_mode: bool, **kw: Any) -> AuditLog:
    """The audit log of a hub state dir (``<state>/audit.db``), signing with the key store's current ``HK_op``."""
    return AuditLog(
        AuditStore(str(state / AUDIT_FILE)),
        keys.op_signer,
        keys.hub_id,
        lambda: keys.op_cert()["o"]["cert_id"],
        op_cert_by_id=keys.op_cert_by_id,
        dev_mode=dev_mode,
        **kw,
    )
