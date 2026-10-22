"""Custodial events on the host (hub contract section 8, phase 4, C3): the verification a host runs on an event that
carries ``auth: "hub"`` BEFORE it may enter a log.

Written from the contract text (D79: no hub code is imported). :func:`verify_custodial` is section 8.3 in contract order
and a pure function of its arguments; :func:`verify_hub_event` feeds it from the host's :class:`HubState` (the pinned
root, the stored card and revocations, the pinned snapshot) and verifies all of those again now.

What passing this check means: the hub's ``UK`` for a user, bound to the pinned ``HK_root`` through an unrevoked
``HK_op``, signed this exact event for this exact workspace, log and type; the user's channel key signed the digest of
it; the channel is not revoked; the user's role reaches the type's floor. It does NOT mean the event is admitted: the
ticket-format checks of step 9 (``based_on``, ``roster_v``, ``gate_gen``, the gate ``hash``, eligibility and D59
independence) run after this, against the host's own log, with the actor resolved through
:func:`orch.model.identity.hub_actor`.

The registration label ``orch/v2/hub-channel-reg|`` is never a custodial proof: the proof is checked under
``orch/v2/hub-channel-proof|`` only (section 6.3)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from functools import cache
from typing import Any

from orch import schema
from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as _suite
from orch.crypto.encoding import EncodingError, unb64u
from orch.model import Refusal
from orch.model.identity import hub_actor

from . import wire
from .errors import HubRefused
from .snapshot import ROLES, _held, verify_snapshot

__all__ = [
    "CUSTODIAL_TYPES",
    "DECISION_TYPES",
    "Admit",
    "MEMBER_ROLE_FLOOR",
    "accept_event_deliver",
    "check_custodial_role",
    "custodial_label",
    "event_digest",
    "verify_custodial",
    "verify_hub_event",
]

SIG_CHANNEL_PROOF = b"orch/v2/hub-channel-proof|"
_H_EVENT_DIGEST = b"orch/v2/hub-event-digest|"
_CUSTODIAL_PREFIX = b"orch/v2/hub-custodial|"
#: the fields ticket-format section 5.3 leaves out of the signed event; the host adds them when it appends
NOT_SIGNED = ("seq", "at", "prev", "ws_seq", "sig", "host_sig")
HOST_FIELDS = ("seq", "at", "prev", "ws_seq", "host_sig")
#: the fields section 8.1 adds to a person event
HUB_FIELDS = (
    "auth", "channel", "channel_factor", "hub_id", "snapshot_version", "channel_cert", "uk_cert", "channel_proof",
)  # fmt: skip
_EVENT_TYPE = re.compile(r"[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}")
_USER_ACTOR = re.compile(r"u_[0-9a-f]{32}")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_FACTOR_RANK = {"password+totp": 0, "se-unlocked": 1, "passkey": 2}  # D66, spec section 5
_ROLE_RANK = {"read": 1, "edit": 2, "admin": 3}

#: Section 8.5, the closed list: (type, log kind) -> the lowest hub role that may start it. ``read`` starts nothing.
#: Everything else is ``not_custodial``: a host refuses ``auth: "hub"`` for it, even when correctly signed.
CUSTODIAL_TYPES: dict[tuple[str, str], str] = {
    **{
        (t, "ticket"): "edit"
        for t in (
            "ticket.created",
            "ticket.updated",
            "status.changed",
            "ticket.submitted",
            "ticket.closed",
            "ticket.reopened",
            "claim.released",
            "log.added",
            "handoff.written",
            "question.asked",
            "question.answered",
            "gate.approved",
            "gate.changes_requested",
            "verdict.given",
        )
    },  # fmt: skip
    **{(t, "ticket"): "admin" for t in ("ticket.moved", "people.changed", "visibility.changed")},
    **{(t, "workspace"): "admin" for t in ("addon.disabled", "addon.purged")},
}
#: L2P4-2a: a decision names the hash the person reviewed. The hub binds ``action.hash`` to ``event.hash`` byte for
#: byte; the host's side of that is that the field is present on all four (``malformed``) and that step 9 compares it
#: with its own log's current hash.
DECISION_TYPES = frozenset({"gate.approved", "gate.changes_requested", "verdict.given", "question.answered"})
#: L2P4-1, selective mode: the F1 member role of the user's canonical person -> the role floor of step 7b.
MEMBER_ROLE_FLOOR = {"owner": "admin", "maintainer": "edit", "member": "edit", "viewer": "read"}


def log_kind(log: str) -> str:
    return "workspace" if log == "workspace" else "ticket"


def check_custodial_role(event_type: Any, log: str, role: Any) -> None:
    """Step 7b: the type is custodial for this log, and the role reaches its floor."""
    need = CUSTODIAL_TYPES.get((event_type, log_kind(log))) if type(event_type) is str else None
    if need is None:
        raise HubRefused("not_custodial")
    if role not in _ROLE_RANK or _ROLE_RANK[role] < _ROLE_RANK[need]:
        raise HubRefused("forbidden_role")


def custodial_label(typ: str) -> bytes:
    """``orch/v2/hub-custodial|<type>|``; the grammar keeps ``|`` out of ``type`` (``malformed``)."""
    if type(typ) is not str or _EVENT_TYPE.fullmatch(typ) is None:
        raise HubRefused("malformed")
    return _CUSTODIAL_PREFIX + typ.encode("ascii") + b"|"


def _context(workspace_id: str, log: str, e: Mapping[str, Any]) -> bytes:
    """``cj(ctx(E))`` of section 8.2 (ticket-format 5.3's context)."""
    try:
        return cj({"contract": 1, "suite": wire.SUITE_ID, "workspace_id": workspace_id, "log": log, "event": dict(e)})
    except (JcsError, ValueError):
        raise HubRefused("malformed") from None


def event_digest(workspace_id: str, log: str, e_signed: Mapping[str, Any]) -> bytes:
    """What the channel key signs: ``H("orch/v2/hub-event-digest|" || cj(ctx(E without "channel_proof")))``."""
    e = {k: v for k, v in e_signed.items() if k != "channel_proof"}
    return _suite.sha256(_H_EVENT_DIGEST + _context(workspace_id, log, e))


@cache
def _payload_spec(typ: str) -> tuple[frozenset[str], frozenset[str]]:
    """The exact payload of a custodial type, from core's own event schema (additionalProperties is false there, so a
    field the schema does not list is a smuggled one): (allowed, required) top-level names."""
    sch = schema.load("event." + typ)
    return frozenset(sch["properties"]), frozenset(sch["required"])


def _payload_shape(typ: str, event: Mapping[str, Any]) -> None:
    """Exact field set of the event type, plus section 8.1's hub fields. A field the type does not carry, or a missing
    one, is ``malformed`` (field smuggling); the VALUES are the ticket-format checks of step 9."""
    try:
        allowed, required = _payload_spec(typ)
    except KeyError:
        raise HubRefused("not_custodial") from None
    host = frozenset(HOST_FIELDS)
    if not set(event) <= (allowed | set(HUB_FIELDS)):
        raise HubRefused("malformed")
    if not (required - host) <= set(event):
        raise HubRefused("malformed")
    if typ in DECISION_TYPES and type(event.get("hash")) is not str:
        raise HubRefused("malformed")  # L2P4-2a: the reviewed hash is always present


def verify_custodial(
    event: Any,
    *,
    workspace_id: str,
    log: str,
    held: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    now_ms: int,
    dev_mode: bool,
    mode: str = "full",
    granted_users: frozenset[str] = frozenset(),
    member_roles: Mapping[str, str] = {},
    min_factor: str,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Section 8.3 steps 1-8, in order. ``event`` is the event as delivered (``E`` plus ``sig``; no host fields).
    ``held`` is the result of :func:`wire.verify_card` (the card verified through the pinned root with the revoked
    op certs applied: the chain ``UK -> HK_op cert -> pinned HK_root``). ``snapshot`` is the object of the newest
    verified snapshot. Returns ``{"user_id", "channel", "role", "floor", "factor"}``: ``role`` is the hub snapshot's
    (informational; hub roles grant nothing in selective mode), ``floor`` the role step 7b used. Step 9 is the caller's.

    Selective mode (D80): hub roles grant nothing; ``granted_users`` are the users the owner granted at the host
    terminal and ``member_roles`` maps a granted user id to the F1 member role of their canonical person (absent: a
    ``viewer``, who signs nothing, L2P4-1)."""
    if type(event) is not dict or any(k in event for k in HOST_FIELDS):
        raise HubRefused("malformed")
    if mode not in wire.MODES or (log != "workspace" and (type(log) is not str or _ULID.fullmatch(log) is None)):
        raise HubRefused("malformed")
    if not wire.hex16(workspace_id):
        raise HubRefused("malformed")
    # 1. shape of the hub fields
    if event.get("auth") != "hub" or any(k not in event for k in (*HUB_FIELDS, "sig", "type", "actor")):
        raise HubRefused("malformed")
    actor, typ = event["actor"], event["type"]
    custodial_label(typ)  # the type grammar (malformed)
    if (
        type(actor) is not dict
        or set(actor) != {"kind", "id", "via"}
        or actor["kind"] != "person"
        or type(actor["id"]) is not str
        or _USER_ACTOR.fullmatch(actor["id"]) is None
        or type(actor["via"]) is not str
        or actor["via"] != event["channel"]
    ):
        raise HubRefused("malformed")
    if (
        not wire.hex16(event["channel"])
        or type(event["channel_factor"]) is not str
        or event["channel_factor"] not in wire.FACTORS
        or not wire.hex16(event["hub_id"])
        or type(event["snapshot_version"]) is not int
        or event["snapshot_version"] < 1
    ):
        raise HubRefused("malformed")
    user = actor["id"][2:]
    # 2. the hub this workspace is bound to
    if event["hub_id"] != held["card"]["hub_id"]:
        raise HubRefused("other_hub")
    # 3. UK -> HK_op cert -> pinned HK_root: held was verified against the pin and the revocations, and is checked
    #    against the CURRENT revoked set again inside verify_uk_cert
    uk = wire.verify_uk_cert(
        event["uk_cert"], held, now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked_op_certs
    )
    if uk["user_id"] != user:
        raise HubRefused("other_user")
    # 4. the channel certificate
    ch = wire.verify_channel_cert(
        event["channel_cert"], held, now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked_op_certs
    )
    if ch["user_id"] != user or ch["channel_id"] != event["channel"]:
        raise HubRefused("other_user")
    if ch["factor"] != event["channel_factor"]:
        raise HubRefused("factor_mismatch")
    # 5. the UK signature under hub-custodial|<type>|
    e_signed = {k: v for k, v in event.items() if k not in NOT_SIGNED}
    try:
        sig = unb64u(event["sig"], _suite.SIG_LEN)
        proof = unb64u(event["channel_proof"], _suite.SIG_LEN)
    except EncodingError:
        raise HubRefused("malformed") from None
    if not _suite.verify(unb64u(uk["uk_pub"], 65), sig, custodial_label(typ) + _context(workspace_id, log, e_signed)):
        raise HubRefused("bad_signature")
    # 6. the channel proof over the event digest, under the PROOF label only (never the registration label)
    if not _suite.verify(
        unb64u(ch["key_pub"], 65), proof, SIG_CHANNEL_PROOF + event_digest(workspace_id, log, e_signed)
    ):
        raise HubRefused("bad_channel_proof")
    # 7. the snapshot
    if snapshot["workspace_id"] != workspace_id:
        raise HubRefused("other_workspace")
    if event["snapshot_version"] > snapshot["version"]:
        raise HubRefused("unknown_snapshot")
    if event["channel"] in snapshot["revoked_channels"]:
        raise HubRefused("channel_revoked")
    role = snapshot["roles"].get(user)
    if mode == "selective":
        if user not in granted_users:
            raise HubRefused("not_granted")
        floor = MEMBER_ROLE_FLOOR.get(member_roles.get(user, "viewer"))  # L2P4-1; unknown role -> forbidden_role
    else:
        if role not in ROLES:
            raise HubRefused("forbidden_role")
        floor = role
    # 7b. the closed list of custodial types and the lowest role per type
    check_custodial_role(typ, log, floor)
    # 8. D66 policy on the factor: secure-enclave policies are met by passkey only, D49 by se-unlocked or passkey
    #    ``min_factor`` is required (L2-C3-3: no default, so no caller skips this step by forgetting it); the weakest
    #    value, ``password+totp``, is "no requirement", and an unknown policy value refuses.
    if min_factor not in _FACTOR_RANK or _FACTOR_RANK[event["channel_factor"]] < _FACTOR_RANK[min_factor]:
        raise HubRefused("factor_too_weak")
    _payload_shape(typ, event)
    return {
        "user_id": user,
        "channel": event["channel"],
        "role": role,
        "floor": floor,
        "factor": event["channel_factor"],
    }


def verify_hub_event(
    state: Any,
    event: Any,
    *,
    log: str,
    now_ms: int,
    dev_mode: bool,
    granted_users: frozenset[str] = frozenset(),
    member_roles: Mapping[str, str] = {},
    min_factor: str,
) -> dict[str, Any]:
    """:func:`verify_custodial` for the hub this host is bound to. The card, the revoked set and the pinned snapshot
    are verified again NOW (fail closed: a missing snapshot, an expired or revoked op cert, or a card that no longer
    verifies refuses the event). Returns what :func:`verify_custodial` returns."""
    if state is None or state.status != "bound":
        raise HubRefused("not_bound")
    if state.snapshot is None:
        raise HubRefused("unknown_snapshot")
    workspace_id = state.binding["o"]["workspace_id"]
    revoked = frozenset(state.revoked_op_certs)
    held = _held(state, now_ms, dev_mode)
    pinned = verify_snapshot(
        state.snapshot, held, workspace_id=workspace_id, dev_mode=dev_mode, pinned=None, revoked_op_certs=revoked
    )
    return verify_custodial(
        event, workspace_id=workspace_id, log=log, held=held, snapshot=pinned, now_ms=now_ms, dev_mode=dev_mode,
        mode=state.mode, granted_users=granted_users, member_roles=member_roles, min_factor=min_factor,
        revoked_op_certs=revoked,
    )  # fmt: skip


#: ``admit(actor, verified, log, event)``: the host's step 9. ``actor`` is the canonical identity from
#: :func:`orch.model.identity.hub_actor` (``p_<person>`` or ``u_<user>``), ``verified`` what :func:`verify_custodial`
#: returned. It runs the ticket-format checks against the host's own log and appends, or raises :class:`HubRefused`.
Admit = Callable[[str, dict[str, Any], str, dict[str, Any]], None]


def accept_event_deliver(
    state: Any,
    body: Any,
    *,
    ws: Any,
    now_ms: int,
    dev_mode: bool,
    admit: Admit | None,
    granted_users: frozenset[str] = frozenset(),
    min_factor: str,
) -> dict[str, Any]:
    """The host's answer to an h2w ``event_deliver`` (protocol 10.2): ``body`` is ``{"log", "event"}``. Verifies the
    custodial event fully (:func:`verify_hub_event`), resolves its actor through ``identity.hub_actor`` (a hub may not
    mint another hub's user ids), then hands it to ``admit``. Raises :class:`HubRefused` with the code to acknowledge
    with; returns the verified facts on success. Delivery is at least once: ``admit`` deduplicates on the event id.

    Selective mode (D80) is refused (``wrong_mode``): a selective workspace's custodial events bind a shared item's
    hash (spec 8a), and contract 8.1 has no field for it yet (C6's ``item_hash``, open point). Without ``admit``
    nothing is accepted (``unavailable``): verification alone never puts an event in a log."""
    if type(body) is not dict or set(body) != {"log", "event"} or type(body["log"]) is not str:
        raise HubRefused("malformed")
    if state is None or state.status != "bound":
        raise HubRefused("not_bound")
    if state.mode != "full":
        raise HubRefused("wrong_mode")
    if admit is None:
        raise HubRefused("unavailable")
    log, event = body["log"], body["event"]
    verified = verify_hub_event(
        state, event, log=log, now_ms=now_ms, dev_mode=dev_mode, granted_users=granted_users,
        member_roles={}, min_factor=min_factor,
    )  # fmt: skip
    who = hub_actor(ws, state.hub_id, verified["user_id"])
    if isinstance(who, Refusal):
        raise HubRefused("other_user")
    admit(who, verified, log, event)
    return verified
