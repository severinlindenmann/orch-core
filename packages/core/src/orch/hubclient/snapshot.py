"""The group snapshot on the host (hub contract section 7): verify, pin, and read roles.

``verify_snapshot`` is section 7 in contract order, under the op key of the hub card the host already verified.
:func:`refresh` fetches the newest snapshot over the link (``snapshot_get``, section 10.2), verifies it and pins it in
the :class:`~orch.hubclient.service.HubState`. The pin only ever grows: a lower version is ``stale_snapshot``, the same
version with other bytes is ``snapshot_conflict``. :func:`hub_role` reads a role from the pinned copy and re-verifies it
on every read (fail closed: a card that is expired, revoked or not verifiable gives no role).

Phase 3 (V3-D3): roles are used for hub-side reads, routes and shared spaces only. They grant no signature, and
custodial events (section 8) are not verified here (phase 4)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import wire
from .errors import HubRefused
from .link import HubLink
from .marketpolicy import MarketPolicy, parse_policy
from .service import HubState

role_at = wire.role_at

__all__ = [
    "ROLES",
    "hub_market_policy",
    "hub_role",
    "pinned_view",
    "record_revoked_op",
    "refresh",
    "role_at",
    "verify_snapshot",
]

ROLES = ("read", "edit", "admin")


def _held(state: HubState, now_ms: int, dev_mode: bool, card: Any = None) -> dict[str, Any]:
    """The card the host holds, verified again now against the pinned root and the revoked set (never trusted from
    the file as it stands). ``card`` is a newer card to test against the stored one."""
    if state.card is None:
        raise HubRefused("no_card")
    root = wire.unb64u(state.hk_root_pub, 65)
    revoked = frozenset(state.revoked_op_certs)
    kw = {"now_ms": now_ms, "dev_mode": dev_mode, "pinned_root": root, "revoked_op_certs": revoked}
    if card is None:
        return wire.verify_card(state.card, **kw)
    # a newer card replaces the held one, but is ordered against it by op_seq / card_seq; the held one need not be
    # fresh (an expired op cert is exactly why a host asks for a new card), so it is read without the time check
    try:
        then = max(state.card["o"]["issued_ms"], state.card["op_cert"]["o"]["issued_ms"])
    except (KeyError, TypeError):
        raise HubRefused("malformed") from None
    # the ordering base is the held card as it was: usually the very op cert the new card replaces, and that one is
    # revoked by now, so the revoked set applies to the NEW card only
    old = wire.verify_card(state.card, **{**kw, "now_ms": then, "revoked_op_certs": frozenset()})
    return wire.verify_card(card, **kw, held=old)


def adopt_card(state: HubState, hub_card: Any, *, now_ms: int, dev_mode: bool) -> bool:
    """Replace the held card by a newer one of the same hub (a rotated op cert, L2-1): ordered by ``op_seq`` then
    ``card_seq``, checked against the revoked set, dev marker and time. Updates the op key and the TLS pin the next
    link uses. True when something changed (an identical card is a no-op). The caller saves."""
    held = _held(state, now_ms, dev_mode, hub_card)
    if state.card == hub_card:
        return False
    state.card, state.hk_op_pub = hub_card, wire.b64u(held["hk_op_pub"])
    state.tls_spki = held["card"]["tls_spki_sha256"]
    return True


def verify_snapshot(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Section 7, in order. ``held`` is the result of :func:`wire.verify_card`; ``pinned`` is the object (``o``) of the
    snapshot the host holds. Returns the object to hold next (the pinned one for an identical re-send)."""
    if held["op_cert"]["cert_id"] in revoked_op_certs:  # the CURRENT revoked set, at every use (L2-1)
        raise HubRefused("revoked")
    o = wire._envelope(signed, "hub.snapshot")  # noqa: SLF001 - the one envelope/signature implementation
    wire._signature(signed, wire.SIG_SNAPSHOT, held["hk_op_pub"])  # noqa: SLF001
    wire._fields(o, "hub.snapshot")  # noqa: SLF001
    if o["hub_id"] != held["card"]["hub_id"]:
        raise HubRefused("other_hub")
    if o["op_cert_id"] != held["op_cert"]["cert_id"]:
        raise HubRefused("stale_op_cert")
    wire.check_dev(o["dev"], dev_mode)
    roles, revoked = o["roles"], o["revoked_channels"]
    if any(type(r) is not str or r not in ROLES or not wire.hex16(u) for u, r in roles.items()):
        raise HubRefused("malformed")
    if any(not wire.hex16(c) for c in revoked) or revoked != sorted(set(revoked)):
        raise HubRefused("malformed")
    ends = o["ends_ms"]  # GA-07: only users that have a role; each end is an integer after the issue time
    if any(
        not wire.hex16(u) or u not in roles or type(t) is not int or not o["issued_ms"] < t < 2**53
        for u, t in ends.items()
    ):
        raise HubRefused("malformed")
    if o["workspace_id"] != workspace_id:
        raise HubRefused("other_workspace")
    if pinned is not None:
        if o["version"] < pinned["version"]:
            raise HubRefused("stale_snapshot")
        if o["version"] == pinned["version"]:
            if wire.cj(o) != wire.cj(dict(pinned)):
                raise HubRefused("snapshot_conflict")
            return dict(pinned)
    return o


def _accept(state: HubState, signed: Any, *, workspace_id: str, now_ms: int, dev_mode: bool) -> bool:
    """Verify ``signed`` against the held card and the pin; pin it. True when the pin changed."""
    held = _held(state, now_ms, dev_mode)
    pinned = None if state.snapshot is None else state.snapshot["o"]
    o = verify_snapshot(
        signed, held, workspace_id=workspace_id, dev_mode=dev_mode, pinned=pinned,
        revoked_op_certs=frozenset(state.revoked_op_certs),
    )  # fmt: skip
    if pinned is not None and o["version"] == pinned["version"]:
        return False
    if o["version"] < state.snapshot_floor:  # GA-07: never go below the highest version seen, also after a rebind
        raise HubRefused("stale_snapshot")
    state.snapshot = signed
    state.snapshot_floor = max(state.snapshot_floor, o["version"])
    return True


def record_revoked_op(state: HubState, signed: Any) -> bool:
    """Section 4.2: add a root-signed ``hub.op_revoked`` to the host's revoked set, for good. True when it was new."""
    o = wire.verify_op_revoked(signed, wire.unb64u(state.hk_root_pub, 65))
    if o["cert_id"] in state.revoked_op_certs:
        return False
    state.revoked_op_certs = sorted({*state.revoked_op_certs, o["cert_id"]})
    state.revoked_op_objs[o["cert_id"]] = signed
    return True


def refresh(
    *,
    state: HubState,
    link: HubLink,
    workspace_id: str,
    dev_mode: bool,
    now_ms: Callable[[], int],
    save: Callable[[HubState], None],
    hub_card: Any = None,
    card_adopted: bool = False,
) -> bool:
    """``snapshot_get`` over ``link``, verify, pin, save. ``hub_card`` (optional) is a newer hub card to adopt first
    (a rotated op cert): the link then verifies replies under the NEW op key (the TLS pin of the new card matters for
    the next dial; a caller that dials after the swap should call :func:`adopt_card` itself and pass
    ``card_adopted=True``). Returns True when the state changed. A refusal leaves the old pin untouched."""
    if state.status != "bound":
        raise HubRefused("not_joined")
    changed = card_adopted
    if hub_card is not None:
        changed = adopt_card(state, hub_card, now_ms=now_ms(), dev_mode=dev_mode) or changed
    if changed:
        link.set_hk_op(wire.unb64u(state.hk_op_pub, 65))
    body = link.call("snapshot_get", {})
    if set(body) != {"snapshot"}:
        raise HubRefused("malformed")
    changed = _accept(state, body["snapshot"], workspace_id=workspace_id, now_ms=now_ms(), dev_mode=dev_mode) or changed
    if changed:
        save(state)
    return changed


def pinned_view(state: HubState | None) -> dict[str, Any] | None:
    """``{"version", "roles"}`` of the pinned copy as stored (the status shape), or None. Not a role decision: use
    :func:`hub_role`, which verifies."""
    if state is None or state.snapshot is None:
        return None
    o = state.snapshot["o"]
    return {"version": o["version"], "roles": dict(sorted(o["roles"].items()))}


def hub_role(state: HubState | None, user_id: str, *, now_ms: int, dev_mode: bool) -> str | None:
    """``read`` | ``edit`` | ``admin`` | ``None`` for a hub user id, from the pinned snapshot. The card and the
    snapshot are verified again now (signature, op cert not expired or revoked, dev marker, workspace); any failure,
    and any user without a role, is ``None``."""
    if state is None or state.snapshot is None or not wire.hex16(user_id) or state.status != "bound":
        return None
    try:
        held = _held(state, now_ms, dev_mode)
        o = verify_snapshot(
            state.snapshot, held, workspace_id=state.binding["o"]["workspace_id"], dev_mode=dev_mode, pinned=None,
            revoked_op_certs=frozenset(state.revoked_op_certs),
        )  # fmt: skip
    except (HubRefused, KeyError, TypeError):
        return None
    return wire.role_at(o, user_id, now_ms)


def hub_market_policy(state: HubState | None, *, now_ms: int, dev_mode: bool) -> tuple[int, MarketPolicy] | None:
    """The hub marketplace policy of the pinned snapshot, the read-only accessor of the market package (D14).

    Two shapes, never mixed: ``None`` means exactly "this host is not hub-joined" (no hub state, or status ``unbound``),
    so the owner decides alone (D79). Every other state is joined, and then the result is ``(snapshot version,
    policy)`` or **:class:`HubRefused`**: no snapshot pinned yet, status ``joining`` or ``switching``, an expired or
    revoked card, a bad signature, dev marker, workspace or policy. A caller therefore cannot read a failure as
    "standalone" (fail closed); it maps a refusal to ``market.not_allowed``. The card and the snapshot are verified
    again on every read, as in :func:`hub_role`. Never fetches; for a fresh one call :func:`refresh` first."""
    if state is None or state.status == "unbound":
        return None
    if state.status != "bound":
        raise HubRefused("not_bound")
    if state.snapshot is None:
        raise HubRefused("no_snapshot")
    try:
        held = _held(state, now_ms, dev_mode)
        o = verify_snapshot(
            state.snapshot, held, workspace_id=state.binding["o"]["workspace_id"], dev_mode=dev_mode, pinned=None,
            revoked_op_certs=frozenset(state.revoked_op_certs),
        )  # fmt: skip
        return o["version"], parse_policy(o["market_policy"])
    except (KeyError, TypeError):
        raise HubRefused("malformed") from None
