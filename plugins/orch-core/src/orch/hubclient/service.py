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

__all__ = ["HubState", "JoinResult", "LeaveResult", "flush_outbox", "join_selective", "leave"]

STATUSES = ("joining", "bound", "unbound")


_ENTRY = {"unbound", "addr", "tls_spki", "hk_op_pub", "mode"}


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
    tls_spki: str | None = None  # b64u SPKI hash from the verified hub card: the pin of that dial

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
            "tls_spki": self.tls_spki,
        }

    @classmethod
    def from_json(cls, d: Any) -> HubState:
        """Strict: anything unreadable is ``malformed`` (fail closed)."""
        keys = {"hub_id", "hk_root_pub", "hk_op_pub", "mode", "binding_seq", "status", "binding", "aliases"}
        if type(d) is not dict or set(d) - {"outbox", "addr", "tls_spki"} != keys:
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
            and (d.get("tls_spki") is None or type(d["tls_spki"]) is str)
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
            tls_spki=d.get("tls_spki"),
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
    addr: str | None = None,
    old_transport: Callable[[str, dict[str, Any]], HubTransport],
) -> JoinResult:
    if mode != "selective":
        raise HubRefused("full_mode_later")
    if type(invite) is not str or not invite or len(invite) > 512:
        raise HubRefused("malformed")
    if state is not None and state.status == "bound":
        if binding_seq is None or binding_seq <= state.binding_seq:
            raise HubRefused("already_joined")
        # the log says left (and joined again) but the hub was never told: owe it the unbound of the old binding
        _owe(state, workspace_id, sign_wsk, now_ms, dev=bool(state.binding["o"]["dev"]))
        state.status = "unbound"
        save(state)
    pinned = None if state is None or state.status == "unbound" else unb64u(state.hk_root_pub, 65)
    verified = wire.verify_card(hub_card, now_ms=now_ms(), dev_mode=dev_mode, pinned_root=pinned)
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
        binding, payload = state.binding, {}
        new = state
        new.addr = addr or new.addr  # a retry may name the address the first try did not have
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
            addr=addr,
            tls_spki=verified["card"]["tls_spki_sha256"],
            outbox={} if state is None else state.outbox,
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


def _owe(state: HubState, workspace_id: str, sign_wsk: wire.Sign, now_ms: Callable[[], int], *, dev: bool) -> dict:
    """Sign the ``hub.unbound`` of the state's current binding and put it in the outbox under its hub (a newer one for
    the same hub replaces an older one)."""
    unbound = wire.build_unbound(
        workspace_id=workspace_id, hub_id_=state.hub_id, binding_seq=state.binding_seq,
        unbound_ms=now_ms(), dev=dev, sign=sign_wsk,
    )  # fmt: skip
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
            ).call("binding_put", {"object": entry["unbound"]})  # fmt: skip
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
) -> LeaveResult:
    """Break-glass leave: ``hub.left`` is logged, ``hub.unbound`` is signed with ``WSK``, stored in the outbox and
    offered to its hub once; an unreachable hub does not stop it. Works from ``joining`` too."""
    if state is None or state.status == "unbound":
        raise HubRefused("not_joined")
    payload = wire.left_payload(hub_id_=state.hub_id, binding_seq=state.binding_seq)
    record_left(payload)
    unbound = _owe(state, workspace_id, sign_wsk, now_ms, dev=dev_mode)
    state.status = "unbound"
    save(state)
    flush_outbox(state=state, workspace_id=workspace_id, sign_wsk=sign_wsk, now_ms=now_ms,
                 transport_for=transport_for, save=save)  # fmt: skip
    return LeaveResult(state, payload, unbound, state.hub_id not in state.outbox)
