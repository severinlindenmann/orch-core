"""The ``hub.publish_where`` a host holds for its bound hub (#1838, protocol 10.9.1): what is kept, when it is dropped
and the seq tombstone. :mod:`orch.hubclient.publish_where` verifies one object; this module is the state around it,
on a :class:`~orch.hubclient.service.HubState` (``publish_where`` and ``publish_where_floors``). No I/O: the caller
persists the state when a function says it changed.

* **One verifier, one store.** :func:`offer` takes an object from a join answer or a ``hub_where`` answer, verifies it
  against the CURRENT pinned card and keeps it only if the seq rule allows (higher, or equal with the same bytes).
* **Re-verified at rest.** :func:`read` runs the verifier again on every read, against the card now pinned (its op
  cert not revoked, not expired). An object that no longer verifies is deleted. Transient failures (a fetch error,
  a lower seq, a bad signature on a NEW object) never reach the held one: :func:`offer` refuses the new object and
  leaves the old.
* **Tombstone.** ``publish_where_floors[hub_id]`` is ``{seq, digest}``: the highest seq ever stored and the sha256 of
  the canonical bytes of the object stored at it. It is kept when the object is deleted (leave, unbind) and carried
  into a later binding, so an older object is never taken again, and neither is another object at the same seq (the
  equal-seq rule holds across a leave, a rejoin and a drop at rest). A floor written before digests existed was a bare
  seq: it reads as a seq with no digest, so an equal seq is refused (the hub's next change has a higher one)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto.encoding import unb64u

from . import wire
from .errors import HubRefused
from .publish_where import SEQ_MAX, check_publish_where

__all__ = ["SEQ_MAX", "digest_of", "drop", "floor_ok", "floors_from_json", "floors_of", "offer", "read"]

_LOG = logging.getLogger("orch.hubclient.publish_held")
_HEX64 = frozenset("0123456789abcdef")


def digest_of(signed: Any) -> str:
    """sha256 (hex) of the canonical JSON of a signed object as received: what a floor remembers of it."""
    return hashlib.sha256(cj(signed)).hexdigest()


def floor_ok(v: Any) -> bool:
    """Is ``v`` a floor as stored: ``{seq, digest}`` (``digest`` ``None`` or 64 lower-case hex), or the bare int seq of
    an older file?"""
    if type(v) is int:
        return 0 <= v <= SEQ_MAX
    if type(v) is not dict or set(v) != {"seq", "digest"}:
        return False
    d = v["digest"]
    return (
        type(v["seq"]) is int
        and 0 <= v["seq"] <= SEQ_MAX
        and (d is None or (type(d) is str and len(d) == 64 and _HEX64.issuperset(d)))
    )


def floors_from_json(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The floors as the state keeps them, ``{hub_id: {seq, digest}}``; a bare int (an older file) has no digest."""
    return {
        k: {"seq": v, "digest": None} if type(v) is int else {"seq": v["seq"], "digest": v["digest"]}
        for k, v in d.items()
    }


def floors_of(state: Any) -> dict[str, dict[str, Any]]:
    """Every floor ``state`` holds, with the object it holds counted. ``{}`` for no state."""
    if state is None:
        return {}
    out = {k: dict(v) for k, v in state.publish_where_floors.items()}
    held = state.publish_where
    if held is not None:
        try:
            seq, digest = held["o"]["seq"], digest_of(held)
        except (KeyError, TypeError, JcsError):
            return out
        cur = out.get(state.hub_id)
        if cur is None or cur["seq"] < seq or (cur["seq"] == seq and cur["digest"] is None):
            out[state.hub_id] = {"seq": seq, "digest": digest}
    return out


def _card_key(state: Any, now_ms: int) -> tuple[bytes, bool] | None:
    """``(op_pub, dev)`` of the card pinned NOW, or ``None``: no card, or it does not verify any more (its op cert
    revoked or expired, another root or hub)."""
    if state.card is None:
        return None
    try:
        v = wire.verify_card(
            state.card,
            now_ms=now_ms,
            dev_mode=bool(state.binding["o"]["dev"]),
            pinned_root=unb64u(state.hk_root_pub, 65),
            revoked_op_certs=frozenset(state.revoked_op_certs),
        )
        if v["card"]["hub_id"] != state.hub_id:
            return None
        return v["hk_op_pub"], bool(v["card"]["dev"])
    except (HubRefused, KeyError, TypeError, ValueError):
        return None


def drop(state: Any) -> bool:
    """Delete the held object (the tombstone stays, and counts the object first: ``{seq, digest}``, never a bare int, so
    the same unchanged object is taken again after a rejoin). True when there was one."""
    had = state.publish_where is not None
    if had:
        try:
            state.publish_where_floors = floors_of(state)
        except Exception:  # noqa: BLE001  # a damaged held object must not keep itself: the old floor stays
            _LOG.warning("held publish_where could not be folded into the floor; the old floor is kept", exc_info=True)
    state.publish_where = None
    return had


def read(state: Any, now_ms: int) -> tuple[dict[str, Any] | None, bool]:
    """``(o, changed)``: the held object checked again now, with both origins canonical, or ``None`` when there is none
    or it no longer verifies (then it is deleted and ``changed`` is True: the caller writes the state)."""
    held = state.publish_where
    if held is None:
        return None, False
    key = _card_key(state, now_ms) if state.status == "bound" else None
    o = None
    if key is not None:
        try:
            o = check_publish_where(held, op_pub=key[0], hub_id=state.hub_id, dev=key[1])
        except HubRefused as exc:
            _LOG.info("held publish_where dropped (%s)", exc.code)
    if o is None:
        drop(state)
        return None, True
    return o, False


def offer(state: Any, signed: Any, now_ms: int) -> tuple[str | None, bool]:
    """Verify ``signed`` (a ``hub.publish_where`` as received) and keep it if the seq rule allows. Returns
    ``(refusal code or None, changed)``. A refused object leaves the held one alone (the old stays), except that a held
    object which no longer verifies is deleted first (``changed``). The caller logs a code and writes the state when
    ``changed``."""
    _, changed = read(state, now_ms)
    key = _card_key(state, now_ms) if state.status == "bound" else None
    if key is None:
        return "card_unverified", changed
    try:
        o = check_publish_where(signed, op_pub=key[0], hub_id=state.hub_id, dev=key[1], held=state.publish_where)
    except HubRefused as exc:
        return exc.code, changed
    floor = state.publish_where_floors.get(state.hub_id)
    floor_seq = 0 if floor is None else floor["seq"]
    if o["seq"] < floor_seq:
        return "stale_seq", changed
    try:
        digest = digest_of(signed)
        same = state.publish_where is not None and digest == digest_of(state.publish_where)
    except JcsError:
        return "malformed", changed
    if floor is not None and o["seq"] == floor_seq:
        # the tombstone's bytes decide, held object or not (a held one below the floor is an inconsistent state);
        # a floor with no digest (a bare seq of an older file) refuses an equal seq only when nothing is held
        if floor["digest"] not in (None, digest) or (floor["digest"] is None and state.publish_where is None):
            return "seq_conflict", changed
    if not same:
        state.publish_where = {"o": dict(signed["o"]), "sig": signed["sig"]}
        changed = True
    if floor is None or floor_seq < o["seq"] or floor["digest"] is None:
        state.publish_where_floors[state.hub_id] = {"seq": o["seq"], "digest": digest}
        changed = True
    return None, changed
