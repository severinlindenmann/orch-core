"""Join and leave a hub in selective mode (spec 8, 8a, D80; protocol 9). Orchestration over injected pieces so it is
testable without a host: the workspace's ``WSK`` signer, the callbacks that record the owner-signed log events
(``record_joined``, ``record_left``: the host signs them with the owner's key at its terminal), a persistence
callback and a :class:`~orch.hubclient.link.HubTransport`.

Invariants: selective only (``full_mode_later``); no ``hub`` member and no ``WK_e`` anywhere on this path; the owner's
log event is recorded before anything is sent to the hub; ``leave`` works with the hub down (the signed
``hub.unbound`` is queued in the state) and never resurrects (a later binding needs a higher ``binding_seq``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orch.crypto.encoding import b64u, unb64u

from . import wire
from .errors import HubRefused
from .link import HubLink, HubTransport

__all__ = ["HubState", "JoinResult", "LeaveResult", "flush_unbound", "join_selective", "leave"]

STATUSES = ("joining", "bound", "unbound")


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
    pending_unbound: dict[str, Any] | None = None  # a signed hub.unbound the hub has not acknowledged yet

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
            "pending_unbound": self.pending_unbound,
        }

    @classmethod
    def from_json(cls, d: Any) -> HubState:
        """Strict: anything unreadable is ``malformed`` (fail closed)."""
        keys = {"hub_id", "hk_root_pub", "hk_op_pub", "mode", "binding_seq", "status", "binding", "aliases"}
        if type(d) is not dict or set(d) - {"pending_unbound"} != keys:
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
            and (d.get("pending_unbound") is None or type(d["pending_unbound"]) is dict)
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
            pending_unbound=d.get("pending_unbound"),
        )


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


def join_selective(
    *,
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
    mode: str = "selective",
    binding_seq: int | None = None,
) -> JoinResult:
    if mode != "selective":
        raise HubRefused("full_mode_later")
    if type(invite) is not str or not invite or len(invite) > 512:
        raise HubRefused("malformed")
    pinned = None if state is None or state.status == "unbound" else unb64u(state.hk_root_pub, 65)
    verified = wire.verify_card(hub_card, now_ms=now_ms(), dev_mode=dev_mode, pinned_root=pinned)
    root, hk_op = verified["hk_root_pub"], verified["hk_op_pub"]
    if state is not None and state.status == "bound":
        raise HubRefused("already_joined")
    resume = state is not None and state.status == "joining"
    if resume:
        assert state is not None
        if state.hub_id != verified["card"]["hub_id"]:
            raise HubRefused("other_hub")
        if binding_seq is not None and binding_seq != state.binding_seq:
            raise HubRefused("binding_conflict")
        binding, payload = state.binding, {}
        new = state
    else:
        prior = {} if state is None else state.aliases
        seq = binding_seq or (1 if state is None else state.binding_seq + 1)  # strictly increasing (9.2)
        if state is not None and seq <= state.binding_seq:
            raise HubRefused("stale_binding")
        binding = wire.build_binding(
            workspace_id=workspace_id,
            wsk_pub=wsk_pub,
            hk_root_pub=root,
            mode="selective",
            binding_seq=seq,
            valid_from_ms=now_ms(),
            dev=dev_mode,
            sign=sign_wsk,
        )
        payload = wire.joined_payload(
            hk_root_pub=root, mode="selective", binding_seq=seq, aliases=list(aliases or []), prior_aliases=prior
        )
        merged = dict(prior)
        merged.update({a["person_id"]: a["user_id"] for a in payload["aliases"]})
        new = HubState(
            hub_id=payload["hub_id"],
            hk_root_pub=b64u(root),
            hk_op_pub=b64u(hk_op),
            mode="selective",
            binding_seq=seq,
            status="joining",
            binding=binding,
            aliases=merged,
        )
        record_joined(payload)  # the owner's signature first: nothing leaves the host before it
        save(new)
    link = HubLink(
        transport, hub_id=new.hub_id, workspace_id=workspace_id, hk_op_pub=hk_op, sign=sign_wsk,
        mode="selective", now_ms=now_ms,
    )  # fmt: skip
    link.call("join", {"invite": invite, "card": v2_card, "pk_pub": b64u(pk_pub), "binding": binding})
    new.status = "bound"
    save(new)
    return JoinResult(new, payload)


def _link(state: HubState, workspace_id: str, sign_wsk: wire.Sign, now_ms: Callable[[], int], t: HubTransport):
    return HubLink(
        t, hub_id=state.hub_id, workspace_id=workspace_id, hk_op_pub=unb64u(state.hk_op_pub, 65),
        sign=sign_wsk, mode=state.mode, now_ms=now_ms,
    )  # fmt: skip


def _deliver(state: HubState, workspace_id: str, sign_wsk, now_ms, transport) -> bool:
    assert state.pending_unbound is not None
    try:
        _link(state, workspace_id, sign_wsk, now_ms, transport).call("binding_put", {"object": state.pending_unbound})
    except HubRefused as exc:
        if exc.code == "hub_unreachable":
            return False
        raise
    state.pending_unbound = None
    return True


def leave(
    *,
    state: HubState | None,
    workspace_id: str,
    sign_wsk: wire.Sign,
    dev_mode: bool,
    now_ms: Callable[[], int],
    transport: HubTransport,
    record_left: Callable[[dict[str, Any]], None],
    save: Callable[[HubState], None],
) -> LeaveResult:
    """Break-glass leave: ``hub.left`` is logged, ``hub.unbound`` is signed with ``WSK`` and offered to the hub; an
    unreachable hub does not stop it (the unbound stays queued). Works from ``joining`` too (the join never
    completed)."""
    if state is None or state.status == "unbound":
        raise HubRefused("not_joined")
    payload = wire.left_payload(hub_id_=state.hub_id, binding_seq=state.binding_seq)
    unbound = wire.build_unbound(
        workspace_id=workspace_id, hub_id_=state.hub_id, binding_seq=state.binding_seq,
        unbound_ms=now_ms(), dev=dev_mode, sign=sign_wsk,
    )  # fmt: skip
    record_left(payload)
    state.status, state.pending_unbound = "unbound", unbound
    save(state)
    delivered = False
    try:
        delivered = _deliver(state, workspace_id, sign_wsk, now_ms, transport)
    finally:
        save(state)
    return LeaveResult(state, payload, unbound, delivered)


def flush_unbound(
    *,
    state: HubState,
    workspace_id: str,
    sign_wsk: wire.Sign,
    now_ms: Callable[[], int],
    transport: HubTransport,
    save: Callable[[HubState], None],
) -> bool:
    """Retry a queued ``hub.unbound`` (idempotent: the hub's store takes the same unbound again)."""
    if state.pending_unbound is None:
        return True
    try:
        return _deliver(state, workspace_id, sign_wsk, now_ms, transport)
    finally:
        save(state)
