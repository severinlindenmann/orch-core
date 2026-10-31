"""Join and leave a hub (spec 8, 8a, D74, D80; protocol 9, 10.2). Orchestration over injected pieces so it is testable
without a host: the workspace's ``WSK`` signer, the callbacks that record the owner-signed log events
(``record_joined``, ``record_left``: the host signs them with the owner's key at its terminal), a persistence
callback, a :class:`~orch.hubclient.link.HubTransport` and, for full mode only, an :class:`EpochKeys`.

Invariants: :func:`join_selective` is selective only (``full_mode_later``): no ``hub`` member and no ``WK_e`` on
that path, and a selective link refuses ``wk_seal`` (``wrong_mode``). Only :func:`join_full` and :func:`switch_full`
(full binding, an ``EpochKeys``) ever seal ``WK_e``, and only the *current committed* epoch, only to the ``HXK`` of the
hub's verified card, never after a leave until a newer epoch exists (``rotate_after``). The owner's log event is
recorded before anything is sent to the hub; ``leave`` works with the hub down (the signed ``hub.unbound`` is queued
in the state), requests the epoch rotation of a full hub durably (``rotate_after`` stays until a newer epoch is
committed) and never resurrects (a later binding needs a higher ``binding_seq``)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from orch.crypto.encoding import b64u, unb64u

from . import publish_held, wire
from .errors import HubRefused
from .link import HubLink, HubTransport
from .publish_where import check_publish_where_shape

__all__ = [
    "EpochKeys",
    "retire_full_state",
    "HubState",
    "JoinResult",
    "LeaveResult",
    "flush_outbox",
    "join_full",
    "join_selective",
    "leave",
    "seal_to_hub",
    "settle_rotation",
    "switch_full",
]

#: ``switching``: a selective binding is being replaced by a signed full one the hub has not taken yet (9.4 mode switch)
STATUSES = ("joining", "bound", "unbound", "switching")


class EpochKeys(Protocol):
    """The host's epoch keys, as the full-mode path needs them. The raw ``WK_e`` never crosses this boundary: the
    host seals it itself (``MembershipKeys.sealed_wk``, v2 5.3) and hands out only the sealed wrap."""

    def current_epoch(self) -> int:
        """The committed epoch, or 0 when the workspace holds none."""
        ...

    def seal_to_hub(self, epoch: int, hxk_pub: bytes, hub_id: str) -> bytes:
        """``WK_epoch`` sealed to ``hxk_pub`` (recipient id: the hub id). Refuses any epoch but the committed one."""
        ...

    def rotate(self) -> None:
        """Ask for a new epoch (idempotent, durable). The new key is never sealed to a hub that has left."""
        ...


_ENTRY = {"unbound", "addr", "tls_spki", "hk_op_pub", "mode"}
_OPTIONAL = {
    "outbox", "addr", "tls_spki", "hxk_pub", "sealed", "rotate_after", "card", "revoked_op_certs", "snapshot",
    "revoked_op_objs", "publish_owed", "snapshot_floors", "closing", "publish_where", "publish_where_floors",
}  # fmt: skip
#: the unsigned ``hub_publish`` hint of an earlier build: read and dropped (never kept, never written), so a
#: ``hub.json`` of that build is not refused as unreadable (#1838)
_LEGACY = {"hub_publish"}
_LOG = logging.getLogger("orch.hubclient.service")
MAX_ADDRS = 4  # M4 (#1461): other addresses of the hub the host has verified (next to ``addr``)


def _addrs_ok(v: Any) -> bool:
    from .tlsadapter import parse_addr

    if type(v) is not list or len(v) > MAX_ADDRS or len(set(v)) != len(v):
        return False
    try:
        for a in v:
            parse_addr(a)
    except HubRefused:
        return False
    return True


def _closing_ok(v: Any) -> bool:
    return v is None or (
        type(v) is dict
        and set(v) == {"removal_id", "issued_ms", "ends_ms", "binding_seq"}
        and wire.hex16(v["removal_id"])
        and all(type(v[k]) is int and v[k] >= 0 for k in ("issued_ms", "ends_ms"))
        and type(v["binding_seq"]) is int
        and v["binding_seq"] >= 1
        and v["issued_ms"] < v["ends_ms"]
    )


def _outbox_ok(box: Any) -> bool:
    return type(box) is dict and all(
        wire.hex16(h)
        and type(e) is dict
        and set(e) == _ENTRY
        and type(e["unbound"]) is dict
        and wire.is_pub(e["hk_op_pub"])
        and e["mode"] in wire.MODES
        and (e["addr"] is None or type(e["addr"]) is str)
        and (e["tls_spki"] is None or type(e["tls_spki"]) is str)
        for h, e in box.items()
    )


def _sealed_ok(v: Any) -> bool:
    return v is None or (
        type(v) is dict and set(v) == {"epoch", "hxk_pub"} and type(v["epoch"]) is int and v["epoch"] >= 1
        and wire.is_pub(v["hxk_pub"])
    )  # fmt: skip


@dataclass
class HubState:
    """What the host keeps about its one hub (``hub.json`` in the workspace state, written by the caller)."""

    hub_id: str
    hk_root_pub: str  # b64u, pinned at join
    hk_op_pub: str  # b64u, from the verified card
    mode: str
    binding_seq: int
    status: str  # joining | bound | unbound
    binding: dict[str, Any]  # the signed hub.binding of binding_seq
    aliases: dict[str, str] = field(default_factory=dict)  # person_id -> user_id, owner-signed at join only
    #: hub_id -> {unbound, addr, tls_spki, hk_op_pub, mode}: every signed hub.unbound a hub has not acknowledged.
    #: Per hub, it survives new bindings (even with another hub) and is deleted only once delivered.
    outbox: dict[str, dict[str, Any]] = field(default_factory=dict)
    addr: str | None = None  # host:port the host dials (not signed: the TLS pin of the card is the trust)
    #: M4 (#1461): up to 4 more ``host:port`` of the same hub, each stored only after a TLS handshake AT that address
    #: passed against ``tls_spki`` (never from a card or a hint alone); dialled after ``addr``, in this order
    addrs: list[str] = field(default_factory=list)
    hub_name: str | None = None  # the display name the hub says of itself (``hub_where``); the hub's words, a hint
    tls_spki: str | None = None  # b64u SPKI hash from the verified hub card: the pin of that dial
    hxk_pub: str | None = None  # b64u, from the verified card: the key a full-mode ``WK_e`` is sealed to
    #: full mode only: ``{"epoch", "hxk_pub"}`` of the last ``wk_seal`` the hub accepted (``None``: nothing sealed)
    sealed: dict[str, Any] | None = None
    #: set by a leave from full mode: the newest epoch the hub may hold. Cleared only once a newer epoch is committed;
    #: until then no full join seals anything (the new key must never reach that hub).
    rotate_after: int | None = None
    card: dict[str, Any] | None = (
        None  # the signed hub card (with its op cert) verified at join; snapshots verify under it
    )
    revoked_op_certs: list[str] = field(default_factory=list)  # cert_ids of root-signed hub.op_revoked, never removed
    snapshot: dict[str, Any] | None = None  # the pinned signed hub.snapshot (section 7); its version only grows
    #: GA-07: ``{hub_id: the highest snapshot version ever pinned from that hub}``. It survives a rebind, a mode switch
    #: and joining another hub in between (the new state carries every entry): an older snapshot (other end dates)
    #: is never pinned again (stale_snapshot).
    snapshot_floors: dict[str, int] = field(default_factory=dict)

    @property
    def snapshot_floor(self) -> int:
        """The floor of THIS state's hub."""
        return self.snapshot_floors.get(self.hub_id, 0)

    def raise_floor(self, version: int) -> None:
        self.snapshot_floors[self.hub_id] = max(self.snapshot_floor, version)

    #: cert_id -> the root-signed ``hub.op_revoked`` object itself: what a ``hub.pinned`` line carries (15.2)
    revoked_op_objs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: C8 (design-c8 M2.4): hub_id -> the signed ``hub.unbound`` that orch-publish has not acknowledged yet. A leave
    #: tells publish directly (never through the hub); the entry is deleted once publish took it.
    publish_owed: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: #1463 (protocol 9.7): the verified ``hub.closing`` notice of THIS binding, only its four recorded members
    #: (``verify_closing``'s result). Host-local; cleared by leave, unbind, rebind or a ``closing_get`` answer ``null``.
    closing: dict[str, Any] | None = None
    #: #1838 (protocol 10.9.1): the signed ``hub.publish_where`` of THIS binding as received (``{o, sig}``), kept only
    #: after it verified. Every read goes through :func:`orch.hubclient.publish_held.read`, which checks it again
    #: against the current pinned card; deleted by leave and unbind.
    publish_where: dict[str, Any] | None = None
    #: #1838: ``{hub_id: the highest publish_where seq ever stored from that hub}``, the tombstone. It survives leave
    #: and a rebind (the new state carries every entry): an older object is never stored again (``stale_seq``).
    publish_where_floors: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "hub_id": self.hub_id,
            "hk_root_pub": self.hk_root_pub,
            "hk_op_pub": self.hk_op_pub,
            "mode": self.mode,
            "binding_seq": self.binding_seq,
            "status": self.status,
            "binding": self.binding,
            "aliases": dict(self.aliases),
            "outbox": self.outbox,
            "addr": self.addr,
            "addrs": list(self.addrs),
            "hub_name": self.hub_name,
            "tls_spki": self.tls_spki,
            "hxk_pub": self.hxk_pub,
            "sealed": self.sealed,
            "rotate_after": self.rotate_after,
            "card": self.card,
            "revoked_op_certs": sorted(self.revoked_op_certs),
            "snapshot": self.snapshot,
            "snapshot_floors": {k: self.snapshot_floors[k] for k in sorted(self.snapshot_floors)},
            "revoked_op_objs": {k: self.revoked_op_objs[k] for k in sorted(self.revoked_op_objs)},
            "publish_owed": {k: self.publish_owed[k] for k in sorted(self.publish_owed)},
            "closing": None if self.closing is None else dict(self.closing),
            "publish_where": self.publish_where,
            "publish_where_floors": {k: self.publish_where_floors[k] for k in sorted(self.publish_where_floors)},
        }

    @classmethod
    def from_json(cls, d: Any) -> HubState:
        """Strict: anything unreadable is ``malformed`` (fail closed)."""
        keys = {
            "hub_id", "hk_root_pub", "hk_op_pub", "mode", "binding_seq", "status", "binding", "aliases",
            "addrs", "hub_name",
        }  # fmt: skip
        if type(d) is not dict or set(d) - _OPTIONAL - _LEGACY != keys:
            raise HubRefused("malformed")
        ok = (
            wire.hex16(d["hub_id"])
            and wire.is_pub(d["hk_root_pub"])
            and wire.is_pub(d["hk_op_pub"])
            and d["mode"] in wire.MODES
            and type(d["binding_seq"]) is int
            and d["binding_seq"] >= 1
            and d["status"] in STATUSES
            and type(d["binding"]) is dict
            and type(d["aliases"]) is dict
            and all(wire.hex16(p) and wire.hex16(u) for p, u in d["aliases"].items())
            and _outbox_ok(d.get("outbox", {}))
            and (d.get("addr") is None or (type(d["addr"]) is str and len(d["addr"]) <= 300))
            and _addrs_ok(d["addrs"])
            and (d["hub_name"] is None or (type(d["hub_name"]) is str and len(d["hub_name"]) <= 60))
            and (d.get("tls_spki") is None or type(d["tls_spki"]) is str)
            and (d.get("hxk_pub") is None or wire.is_pub(d["hxk_pub"]))
            and _sealed_ok(d.get("sealed"))
            and (d.get("rotate_after") is None or (type(d["rotate_after"]) is int and d["rotate_after"] >= 0))
            and (d.get("card") is None or type(d["card"]) is dict)
            and type(d.get("revoked_op_certs", [])) is list
            and all(wire.hex16(c) for c in d.get("revoked_op_certs", []))
            and (d.get("snapshot") is None or (type(d["snapshot"]) is dict and type(d["snapshot"].get("o")) is dict))
            and type(d.get("snapshot_floors", {})) is dict
            and all(wire.hex16(k) and type(v) is int and v >= 0 for k, v in d.get("snapshot_floors", {}).items())
            and type(d.get("revoked_op_objs", {})) is dict
            and all(wire.hex16(k) and type(v) is dict for k, v in d.get("revoked_op_objs", {}).items())
            and type(d.get("publish_owed", {})) is dict
            and all(wire.hex16(k) and type(v) is dict for k, v in d.get("publish_owed", {}).items())
            and _closing_ok(d.get("closing"))
            and _publish_where_ok(d.get("publish_where"))
            and type(d.get("publish_where_floors", {})) is dict
            and all(
                wire.hex16(k) and type(v) is int and 0 <= v <= publish_held.SEQ_MAX
                for k, v in d.get("publish_where_floors", {}).items()
            )
            and d["hub_id"] == wire.hub_id(unb64u(d["hk_root_pub"], 65))
        )
        if not ok:
            raise HubRefused("malformed")
        return cls(
            hub_id=d["hub_id"],
            hk_root_pub=d["hk_root_pub"],
            hk_op_pub=d["hk_op_pub"],
            mode=d["mode"],
            binding_seq=d["binding_seq"],
            status=d["status"],
            binding=d["binding"],
            aliases=dict(d["aliases"]),
            outbox=dict(d.get("outbox", {})),
            addr=d.get("addr"),
            addrs=list(d["addrs"]),
            hub_name=d["hub_name"],
            tls_spki=d.get("tls_spki"),
            hxk_pub=d.get("hxk_pub"),
            sealed=None if d.get("sealed") is None else dict(d["sealed"]),
            rotate_after=d.get("rotate_after"),
            card=d.get("card"),
            revoked_op_certs=list(d.get("revoked_op_certs", [])),
            snapshot=d.get("snapshot"),
            snapshot_floors=dict(d.get("snapshot_floors", {})),
            revoked_op_objs=dict(d.get("revoked_op_objs", {})),
            publish_owed=dict(d.get("publish_owed", {})),
            closing=None if d.get("closing") is None else dict(d["closing"]),
            publish_where=d.get("publish_where"),
            publish_where_floors=dict(d.get("publish_where_floors", {})),
        )


def _publish_where_ok(v: Any) -> bool:
    """Shape only (the signature is checked on every read, :func:`orch.hubclient.publish_held.read`)."""
    if v is None:
        return True
    try:
        check_publish_where_shape(v)
    except HubRefused:
        return False
    return True


def _floors_of(state: HubState | None) -> dict[str, int]:
    """Every floor a state holds, with the pinned snapshot of its own hub counted."""
    if state is None:
        return {}
    out = dict(state.snapshot_floors)
    held = 0 if state.snapshot is None else state.snapshot["o"]["version"]
    out[state.hub_id] = max(out.get(state.hub_id, 0), held)
    return out


@dataclass
class JoinResult:
    state: HubState
    payload: dict[str, Any]  # the hub.joined payload recorded (empty when a pending join was resumed)


@dataclass
class LeaveResult:
    state: HubState
    payload: dict[str, Any]  # the hub.left payload
    unbound: dict[str, Any]
    delivered: bool  # False: the hub did not take the unbound yet (queued in the state)
    publish_told: bool = False  # C8: orch-publish holds the unbound (False: still owed in state.publish_owed)
    rotation_done: bool = True  # False: a full hub left and the newer epoch is not committed yet (state.rotate_after)


def join_selective(*, mode: str = "selective", epochs: EpochKeys | None = None, **kw: Any) -> JoinResult:
    """Selective join (D80). Any other mode is refused here: the full path is :func:`join_full` (needs epoch keys)."""
    if mode != "selective":
        raise HubRefused("full_mode_later")
    return _join(mode="selective", epochs=epochs, **kw)  # epochs: only to retire an earlier full state, never to seal


def join_full(*, epochs: EpochKeys, mode: str = "full", **kw: Any) -> JoinResult:
    """Full join (D74, spec 8 step 3): the normal join with a full binding, then ``wk_seal`` of the current ``WK_e``
    to the card's ``HXK``. Refused before anything is sent when the workspace holds no epoch (``no_epoch``) or an
    earlier full hub's rotation is still owed (``rotation_pending``)."""
    if mode != "full":
        raise HubRefused("wrong_mode")
    return _join(mode="full", epochs=epochs, **kw)


def _join(
    *,
    mode: str,
    epochs: EpochKeys | None,
    state: HubState | None,
    invite: str,
    hub_card: Any,
    v2_card: dict[str, Any],
    pk_pub: bytes,
    workspace_id: str,
    wsk_pub: bytes,
    sign_wsk: wire.Sign,
    dev_mode: bool,
    now_ms: Callable[[], int],
    transport: HubTransport,
    record_joined: Callable[[dict[str, Any]], None],
    save: Callable[[HubState], None],
    aliases: list[dict[str, str]] | None = None,
    binding_seq: int | None = None,
    addr: str | None = None,
    old_transport: Callable[[str, dict[str, Any]], HubTransport],
    owner_proof: Mapping[str, Any] | None = None,
) -> JoinResult:
    if type(invite) is not str or not invite or len(invite) > 512:
        raise HubRefused("malformed")
    if owner_proof is None:
        raise HubRefused("proof_missing")  # #1198 C9: the join carries the owner proof from the signed log, never none
    if state is not None and state.status == "bound":
        if binding_seq is None or binding_seq <= state.binding_seq:
            raise HubRefused("already_joined")
        # the log says left (and joined again) but the hub was never told: owe it the unbound of the old binding, and
        # retire a full state first (it owes the rotation whatever comes next, also a selective rejoin)
        _retire_full_state(state, epochs, save, "rejoin")
        _owe(state, workspace_id, sign_wsk, now_ms, dev=bool(state.binding["o"]["dev"]))
        state.status = "unbound"
        state.closing = None
        state.publish_where = None
        save(state)
    if state is not None and state.status == "joining" and binding_seq is not None and binding_seq > state.binding_seq:
        # the log left and joined again (#1198 C9: the owner proof of the first join ran out before the hub took it):
        # the hub never held that binding, so there is nothing to resume and nothing to unbind
        state.status = "unbound"
        state.closing = None
        save(state)
    if mode == "full":
        assert epochs is not None
        _epoch_ready(state, epochs)
    pinned = None if state is None or state.status == "unbound" else unb64u(state.hk_root_pub, 65)
    revoked = frozenset() if state is None else frozenset(state.revoked_op_certs)  # random ids: never collide
    verified = wire.verify_card(
        hub_card, now_ms=now_ms(), dev_mode=dev_mode, pinned_root=pinned, revoked_op_certs=revoked
    )
    root, hk_op = verified["hk_root_pub"], verified["hk_op_pub"]
    if state is not None and state.outbox:
        # every owed unbound gets ONE try over its own hub's transport, then the new join goes on regardless
        flush_outbox(state=state, workspace_id=workspace_id, sign_wsk=sign_wsk, now_ms=now_ms,
                     transport_for=old_transport, save=save)  # fmt: skip
    resume = state is not None and state.status == "joining"
    if resume:
        assert state is not None
        if state.hub_id != verified["card"]["hub_id"]:
            raise HubRefused("other_hub")
        if binding_seq is not None and binding_seq != state.binding_seq:
            raise HubRefused("binding_conflict")
        if state.mode != mode:
            raise HubRefused("binding_conflict")
        binding, payload = state.binding, {}
        new = state
        new.addr = addr or new.addr  # a retry may name the address the first try did not have
        new.hxk_pub = b64u(_hxk(verified))
        new.card = hub_card
    else:
        # a user id means something only on the hub that chose it: another hub starts with no aliases (L2P4-C1)
        prior = state.aliases if state is not None and state.hub_id == wire.hub_id(root) else {}
        seq = binding_seq or (1 if state is None else state.binding_seq + 1)  # strictly increasing (9.2)
        if state is not None and seq <= state.binding_seq:
            raise HubRefused("stale_binding")
        binding = wire.build_binding(
            workspace_id=workspace_id,
            wsk_pub=wsk_pub,
            hk_root_pub=root,
            mode=mode,
            binding_seq=seq,
            valid_from_ms=now_ms(),
            dev=dev_mode,
            sign=sign_wsk,
        )
        payload = wire.joined_payload(
            hk_root_pub=root, mode=mode, binding_seq=seq, aliases=list(aliases or []), prior_aliases=prior,
            owner_proof=owner_proof,
        )  # fmt: skip
        merged = dict(prior)
        merged.update({a["person_id"]: a["user_id"] for a in payload["aliases"]})
        new = HubState(
            hub_id=payload["hub_id"],
            hk_root_pub=b64u(root),
            hk_op_pub=b64u(hk_op),
            mode=mode,
            binding_seq=seq,
            status="joining",
            binding=binding,
            aliases=merged,
            addr=addr,
            tls_spki=verified["card"]["tls_spki_sha256"],
            outbox={} if state is None else state.outbox,
            hxk_pub=b64u(_hxk(verified)),
            rotate_after=None if state is None else state.rotate_after,
            card=hub_card,
            revoked_op_certs=[] if state is None else list(state.revoked_op_certs),  # only ever grows
            snapshot_floors=_floors_of(state),
            publish_where_floors=publish_held.floors_of(state),
        )
        record_joined(payload)  # the owner's signature first: nothing leaves the host before it
        save(new)
    link = HubLink(
        transport, hub_id=new.hub_id, workspace_id=workspace_id, hk_op_pub=hk_op, sign=sign_wsk,
        mode=mode, now_ms=now_ms,
    )  # fmt: skip
    reply = link.call(
        "join",
        {
            "invite": invite,
            "card": v2_card,
            "pk_pub": b64u(pk_pub),
            "binding": binding,
            "owner_proof": dict(owner_proof),
            "alias_users": sorted(new.aliases.values()),  # the owner-signed aliases: the hub holds them to its invite
        },
    )
    if type(reply) is not dict or set(reply) - {"publish_where"}:
        raise HubRefused("malformed")  # the reply is {} or {publish_where}: any other member is not the protocol's
    if "publish_where" in reply:
        check_publish_where_shape(reply["publish_where"])
    new.status = "bound"
    if "publish_where" in reply:  # #1838: kept only if it verifies; a bad one is dropped and the join stands
        code, _ = publish_held.offer(new, reply["publish_where"], now_ms())
        if code is not None:
            _LOG.info("publish_where of the join answer not used (%s)", code)
    save(new)
    if mode == "full":
        assert epochs is not None
        _seal(new, link, epochs, save)
    return JoinResult(new, payload)


def _retire_full_state(
    state: HubState, epochs: EpochKeys | None, save: Callable[[HubState], None], reason: str
) -> bool:
    """THE one place a full-mode state is ended (a leave, an unbind, a rejoin over an un-ended binding, a mode change).
    Invariant: a hub that was ever given ``WK_e`` is never given the next one. So: record the rotation obligation
    (``rotate_after`` = the newest epoch it may hold; the hub member itself goes with the owner's ``hub.left`` in the
    model), forget the seal, and ask for the rotation (the key store's gate, or the local epoch for a relay-less
    workspace). Idempotent; the obligation only grows. Returns True when the state was full."""
    if state.mode != "full":
        return False
    held = state.sealed["epoch"] if state.sealed else 0
    cur = epochs.current_epoch() if epochs is not None else 0
    state.rotate_after = max(held, cur, state.rotate_after or 0)
    state.sealed = None
    save(state)  # durable before anything else happens
    if epochs is not None:
        epochs.rotate()
    return True


retire_full_state = _retire_full_state  # the host calls it when it ends a full state without a leave()


def _hxk(verified: dict[str, Any]) -> bytes:
    hxk = verified["card"].get("hxk_pub")
    if not wire.is_pub(hxk):
        raise HubRefused("malformed")
    return unb64u(hxk, 65)


def _epoch_ready(state: HubState | None, epochs: EpochKeys) -> int:
    """The epoch a full join may seal: the committed one, and only once an earlier full hub's rotation is done."""
    if state is not None and state.rotate_after is not None:
        if epochs.current_epoch() <= state.rotate_after:
            raise HubRefused("rotation_pending")
        state.rotate_after = None  # a newer epoch is committed: the old hub holds only older ones
    e = epochs.current_epoch()
    if type(e) is not int or e < 1:
        raise HubRefused("no_epoch")
    return e


def _seal(state: HubState, link: HubLink, epochs: EpochKeys, save: Callable[[HubState], None]) -> int:
    """Seal the current ``WK_e`` to the state's ``HXK`` and send it (10.2 ``wk_seal``, full links only). Resending the
    same epoch to the same key is idempotent at the hub; a sealed epoch is recorded only after the hub took it."""
    if state.mode != "full" or state.status != "bound" or state.hxk_pub is None:
        raise HubRefused("wrong_mode")
    e = _epoch_ready(state, epochs)
    if state.sealed == {"epoch": e, "hxk_pub": state.hxk_pub}:
        return e
    wrap = epochs.seal_to_hub(e, unb64u(state.hxk_pub, 65), state.hub_id)
    link.call("wk_seal", {"epoch": e, "wrap": b64u(wrap)})
    state.sealed = {"epoch": e, "hxk_pub": state.hxk_pub}
    save(state)
    return e


def seal_to_hub(
    *,
    state: HubState,
    hub_card: Any,
    workspace_id: str,
    sign_wsk: wire.Sign,
    dev_mode: bool,
    now_ms: Callable[[], int],
    transport: HubTransport,
    save: Callable[[HubState], None],
    epochs: EpochKeys,
) -> int:
    """Finish or repeat the seal of a bound full hub (a join whose seal did not get through, or a new epoch). The
    card is verified against the pinned root; its ``HXK`` replaces the stored one."""
    if state.status != "bound" or state.mode != "full":
        raise HubRefused("wrong_mode")
    verified = wire.verify_card(hub_card, now_ms=now_ms(), dev_mode=dev_mode, pinned_root=unb64u(state.hk_root_pub, 65))
    state.hxk_pub = b64u(_hxk(verified))
    link = HubLink(
        transport, hub_id=state.hub_id, workspace_id=workspace_id, hk_op_pub=verified["hk_op_pub"], sign=sign_wsk,
        mode="full", now_ms=now_ms,
    )  # fmt: skip
    return _seal(state, link, epochs, save)


def switch_full(
    *,
    state: HubState | None,
    hub_card: Any,
    workspace_id: str,
    wsk_pub: bytes,
    sign_wsk: wire.Sign,
    dev_mode: bool,
    now_ms: Callable[[], int],
    transport: HubTransport,
    save: Callable[[HubState], None],
    binding_seq: int,
    epochs: EpochKeys,
    owner_proof: Mapping[str, Any] | None = None,
) -> HubState:
    """Selective to full for the hub already bound (9.4 "a mode switch"): sign the full binding, put it (no new
    invite), then seal. The owner's ``hub.joined`` (full) is already on the log. A crash or a down hub leaves
    ``switching``; the same call again resumes with the same signed binding."""
    if state is None or state.status not in ("bound", "switching"):
        raise HubRefused("not_joined")
    if owner_proof is None:
        raise HubRefused("proof_missing")  # #1198 C9: the mode switch carries the owner proof from the signed log
    if state.status == "bound" and state.mode != "selective":
        raise HubRefused("already_joined")
    _epoch_ready(state, epochs)
    verified = wire.verify_card(hub_card, now_ms=now_ms(), dev_mode=dev_mode, pinned_root=unb64u(state.hk_root_pub, 65))
    hk_op = verified["hk_op_pub"]
    if state.status == "switching":
        if binding_seq != state.binding_seq or state.mode != "full":
            raise HubRefused("binding_conflict")
    else:
        if binding_seq <= state.binding_seq:
            raise HubRefused("stale_binding")
        state.binding = wire.build_binding(
            workspace_id=workspace_id, wsk_pub=wsk_pub, hk_root_pub=unb64u(state.hk_root_pub, 65), mode="full",
            binding_seq=binding_seq, valid_from_ms=now_ms(), dev=bool(state.binding["o"]["dev"]), sign=sign_wsk,
        )  # fmt: skip
        state.binding_seq, state.mode, state.status = binding_seq, "full", "switching"
        state.sealed = None
    state.hxk_pub = b64u(_hxk(verified))
    save(state)
    link = HubLink(
        transport, hub_id=state.hub_id, workspace_id=workspace_id, hk_op_pub=hk_op, sign=sign_wsk, mode="selective",
        now_ms=now_ms,
    )  # fmt: skip
    link.call("binding_put", {"object": state.binding, "owner_proof": dict(owner_proof)})  # the hub switches its mode
    state.status = "bound"
    save(state)
    _seal(state, HubLink(transport, hub_id=state.hub_id, workspace_id=workspace_id, hk_op_pub=hk_op, sign=sign_wsk,
                         mode="full", now_ms=now_ms), epochs, save)  # fmt: skip
    return state


def _owe(state: HubState, workspace_id: str, sign_wsk: wire.Sign, now_ms: Callable[[], int], *, dev: bool) -> dict:
    """Sign the ``hub.unbound`` of the state's current binding and put it in the outbox under its hub (a newer one for
    the same hub replaces an older one)."""
    unbound = wire.build_unbound(
        workspace_id=workspace_id, hub_id_=state.hub_id, binding_seq=state.binding_seq,
        unbound_ms=now_ms(), dev=dev, sign=sign_wsk,
    )  # fmt: skip
    state.publish_owed[state.hub_id] = unbound  # publish is told directly too (C8 M2.4)
    held = state.outbox.get(state.hub_id)
    if held is None or held["unbound"]["o"]["binding_seq"] <= state.binding_seq:
        state.outbox[state.hub_id] = {
            "unbound": unbound, "addr": state.addr, "tls_spki": state.tls_spki, "hk_op_pub": state.hk_op_pub,
            "mode": state.mode,
        }  # fmt: skip
    return unbound


def flush_outbox(
    *,
    state: HubState,
    workspace_id: str,
    sign_wsk: wire.Sign,
    now_ms: Callable[[], int],
    transport_for: Callable[[str, dict[str, Any]], HubTransport],
    save: Callable[[HubState], None],
) -> list[str]:
    """One try per owed unbound, each over ITS hub's transport (``transport_for(hub_id, entry)``, required: the
    unbound of one hub is never offered to another). A hub that is down or refuses leaves its entry in place. Returns
    the hub ids that took theirs."""
    done: list[str] = []
    for hub_id, entry in sorted(state.outbox.items()):
        t = transport_for(hub_id, entry)
        try:
            HubLink(
                t, hub_id=hub_id, workspace_id=workspace_id, hk_op_pub=unb64u(entry["hk_op_pub"], 65),
                sign=sign_wsk, mode=entry["mode"], now_ms=now_ms,
            ).call("binding_put", {"object": entry["unbound"], "owner_proof": None})  # fmt: skip
        except HubRefused as exc:
            if exc.code != "stale_binding":  # the hub holds a newer binding: there is nothing left to unbind
                continue
        except OSError:
            continue
        finally:
            getattr(t, "close", lambda: None)()
        done.append(hub_id)
    for hub_id in done:
        state.outbox.pop(hub_id, None)
    if done or state.outbox:
        save(state)
    return done


def flush_publish(
    *, state: HubState, put: Callable[[dict[str, Any]], bool] | None, save: Callable[[HubState], None]
) -> bool:
    """Tell orch-publish directly (C8 M2.4): ``put(unbound)`` is True when publish took it (2xx, or it already holds a
    newer binding). Each owed unbound is tried once here (the caller's client retries a 5xx or a lost answer); what
    publish has not taken stays in ``publish_owed`` and is offered again by the next ``orch hub unbind``. Returns True
    when nothing is owed to publish. ``put=None`` (no publish server set up): nothing can be told, still owed."""
    if put is None:
        return not state.publish_owed
    for hub_id, unbound in sorted(state.publish_owed.items()):
        try:
            took = put(unbound)
        except (OSError, HubRefused):
            took = False
        if took:
            state.publish_owed.pop(hub_id, None)
    save(state)
    return not state.publish_owed


def settle_rotation(
    *, state: HubState, epochs: EpochKeys | None, save: Callable[[HubState], None], request: bool
) -> bool:
    """Is the rotation a full hub's leave owes done? True when none is owed or a newer epoch than the hub could hold
    is committed (then ``rotate_after`` is cleared). Otherwise, with ``request``, ask for the rotation again."""
    if state.rotate_after is None:
        return True
    if epochs is not None and epochs.current_epoch() > state.rotate_after:
        state.rotate_after = None
        save(state)
        return True
    if request and epochs is not None:
        epochs.rotate()
        if epochs.current_epoch() > state.rotate_after:  # a relay-less workspace makes its epoch at once
            state.rotate_after = None
            save(state)
            return True
    return False


def leave(
    *,
    state: HubState | None,
    workspace_id: str,
    sign_wsk: wire.Sign,
    dev_mode: bool,
    now_ms: Callable[[], int],
    transport_for: Callable[[str, dict[str, Any]], HubTransport],
    record_left: Callable[[dict[str, Any]], None],
    save: Callable[[HubState], None],
    epochs: EpochKeys | None = None,
    publish_put: Callable[[dict[str, Any]], bool] | None = None,
) -> LeaveResult:
    """Break-glass leave: ``hub.left`` is logged, ``hub.unbound`` is signed with ``WSK``, stored in the outbox and
    offered to its hub once; an unreachable hub does not stop it. Works from ``joining`` too. From full mode the hub
    member goes (the model, with ``hub.left``) and an epoch rotation is owed: ``rotate_after`` is saved before
    anything else, the rotation is requested at once, and it stays owed (and visible) until a newer epoch is
    committed. The new key is never sealed to this hub."""
    if state is None or state.status == "unbound":
        raise HubRefused("not_joined")
    payload = wire.left_payload(hub_id_=state.hub_id, binding_seq=state.binding_seq)
    record_left(payload)
    unbound = _owe(state, workspace_id, sign_wsk, now_ms, dev=dev_mode)
    _retire_full_state(state, epochs, save, "leave")  # fail closed: even a never-sealed full hub gets a fresh epoch
    state.sealed = None
    state.status = "unbound"
    state.closing = None
    state.publish_where = None  # the held object goes with the binding; the seq tombstone stays
    save(state)
    done = settle_rotation(state=state, epochs=epochs, save=save, request=False)  # the retire already asked
    told = flush_publish(state=state, put=publish_put, save=save)  # publish first: it ends hub-forwarded publish
    flush_outbox(state=state, workspace_id=workspace_id, sign_wsk=sign_wsk, now_ms=now_ms,
                 transport_for=transport_for, save=save)  # fmt: skip
    return LeaveResult(
        state,
        payload,
        unbound,
        state.hub_id not in state.outbox,
        rotation_done=done,
        publish_told=told,
    )
