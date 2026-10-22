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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from orch.crypto.encoding import b64u, unb64u

from . import wire
from .errors import HubRefused
from .link import HubLink, HubTransport

__all__ = [
    "EpochKeys",
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
_OPTIONAL = {"outbox", "addr", "tls_spki", "hxk_pub", "sealed", "rotate_after"}


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
    tls_spki: str | None = None  # b64u SPKI hash from the verified hub card: the pin of that dial
    hxk_pub: str | None = None  # b64u, from the verified card: the key a full-mode ``WK_e`` is sealed to
    #: full mode only: ``{"epoch", "hxk_pub"}`` of the last ``wk_seal`` the hub accepted (``None``: nothing sealed)
    sealed: dict[str, Any] | None = None
    #: set by a leave from full mode: the newest epoch the hub may hold. Cleared only once a newer epoch is committed;
    #: until then no full join seals anything (the new key must never reach that hub).
    rotate_after: int | None = None

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
            "hxk_pub": self.hxk_pub,
            "sealed": self.sealed,
            "rotate_after": self.rotate_after,
        }

    @classmethod
    def from_json(cls, d: Any) -> HubState:
        """Strict: anything unreadable is ``malformed`` (fail closed)."""
        keys = {"hub_id", "hk_root_pub", "hk_op_pub", "mode", "binding_seq", "status", "binding", "aliases"}
        if type(d) is not dict or set(d) - _OPTIONAL != keys:
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
            and (d.get("hxk_pub") is None or wire.is_pub(d["hxk_pub"]))
            and _sealed_ok(d.get("sealed"))
            and (d.get("rotate_after") is None or (type(d["rotate_after"]) is int and d["rotate_after"] >= 0))
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
            hxk_pub=d.get("hxk_pub"),
            sealed=None if d.get("sealed") is None else dict(d["sealed"]),
            rotate_after=d.get("rotate_after"),
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
    rotation_done: bool = True  # False: a full hub left and the newer epoch is not committed yet (state.rotate_after)


def join_selective(*, mode: str = "selective", **kw: Any) -> JoinResult:
    """Selective join (D80). Any other mode is refused here: the full path is :func:`join_full` (needs epoch keys)."""
    if mode != "selective":
        raise HubRefused("full_mode_later")
    return _join(mode="selective", epochs=None, **kw)


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
) -> JoinResult:
    if type(invite) is not str or not invite or len(invite) > 512:
        raise HubRefused("malformed")
    if mode == "full":
        assert epochs is not None
        _epoch_ready(state, epochs)
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
        if state.mode != mode:
            raise HubRefused("binding_conflict")
        binding, payload = state.binding, {}
        new = state
        new.addr = addr or new.addr  # a retry may name the address the first try did not have
        new.hxk_pub = b64u(_hxk(verified))
    else:
        prior = {} if state is None else state.aliases
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
            hk_root_pub=root, mode=mode, binding_seq=seq, aliases=list(aliases or []), prior_aliases=prior
        )
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
        )
        record_joined(payload)  # the owner's signature first: nothing leaves the host before it
        save(new)
    link = HubLink(
        transport, hub_id=new.hub_id, workspace_id=workspace_id, hk_op_pub=hk_op, sign=sign_wsk,
        mode=mode, now_ms=now_ms,
    )  # fmt: skip
    link.call("join", {"invite": invite, "card": v2_card, "pk_pub": b64u(pk_pub), "binding": binding})
    new.status = "bound"
    save(new)
    if mode == "full":
        assert epochs is not None
        _seal(new, link, epochs, save)
    return JoinResult(new, payload)


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
) -> HubState:
    """Selective to full for the hub already bound (9.4 "a mode switch"): sign the full binding, put it (no new
    invite), then seal. The owner's ``hub.joined`` (full) is already on the log. A crash or a down hub leaves
    ``switching``; the same call again resumes with the same signed binding."""
    if state is None or state.status not in ("bound", "switching"):
        raise HubRefused("not_joined")
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
    link.call("binding_put", {"object": state.binding})  # the hub switches its mode with the binding
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
    if state.mode == "full":  # fail closed: even a hub that was never sealed to gets a fresh epoch behind it
        held = state.sealed["epoch"] if state.sealed else 0
        cur = epochs.current_epoch() if epochs is not None else 0
        state.rotate_after = max(held, cur, state.rotate_after or 0)
    state.sealed = None
    state.status = "unbound"
    save(state)
    done = settle_rotation(state=state, epochs=epochs, save=save, request=True)
    flush_outbox(state=state, workspace_id=workspace_id, sign_wsk=sign_wsk, now_ms=now_ms,
                 transport_for=transport_for, save=save)  # fmt: skip
    return LeaveResult(state, payload, unbound, state.hub_id not in state.outbox, done)
