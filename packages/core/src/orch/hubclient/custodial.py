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

import contextlib
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as _suite
from orch.crypto.encoding import EncodingError, unb64u
from orch.model import Refusal
from orch.model.codes import Code
from orch.model.hubledger import ledger_of
from orch.model.identity import role_of
from orch.model.policies import factor_floor

from . import wire
from .custodial_shape import check_custodial_payload
from .errors import HubRefused
from .snapshot import ROLES, _held, verify_snapshot

__all__ = [
    "CUSTODIAL_TYPES",
    "DECISION_TYPES",
    "LandRefused",
    "Landing",
    "RETRYABLE",
    "SELECTIVE_TYPES",
    "MEMBER_ROLE_FLOOR",
    "accept_event_deliver",
    "check_custodial_role",
    "pins_needed",
    "reply_code",
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
_ITEM_HASH = re.compile(r"sha256:[0-9a-f]{64}")
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
    # phase 5 (hub protocol 8.8, owner V3-D29, C10): grants, device exclusion, policy and settings. ``grant.issued`` is
    # NOT custodial: the host makes the secret and appends it itself, naming the ``grant.requested`` it answers.
    # FB15 (8.8b): the owner's ask to turn an add-on on; the host answers with a host-actor ``addon.granted``
    ("addon.requested", "workspace"): "admin",
    ("grant.requested", "workspace"): "edit",
    ("grant.revoked", "workspace"): "edit",
    ("device.excluded", "workspace"): "admin",
    ("policy.changed", "workspace"): "admin",
    ("policy.changed", "ticket"): "edit",
    ("settings.changed", "workspace"): "admin",
}
#: L2P4-2a: a decision names the hash the person reviewed. The hub binds ``action.hash`` to ``event.hash`` byte for
#: byte; the host's side of that is that the field is present on all four (``malformed``) and that step 9 compares it
#: with its own log's current hash.
#: selective mode (D80, 15.7): the only types a person may sign from a shared item
SELECTIVE_TYPES = frozenset({"question.answered", "gate.approved", "gate.changes_requested", "verdict.given"})
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


def _payload_shape(typ: str, event: Mapping[str, Any]) -> None:
    """The exact 8.5 payload of the type (:mod:`.custodial_shape`): allow-list of names and exact nested schemas.
    Runs inside :func:`verify_custodial` before it returns (the shared vectors pin the order of the earlier codes);
    the VALUES against the host's log are the ticket-format checks of step 9."""
    check_custodial_payload(typ, event, frozenset(HUB_FIELDS) | {"sig", "item_hash"})
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
    live_items: Mapping[str, Any] = {},
    superseded_items: frozenset[str] = frozenset(),
    log_heads: Mapping[str, Sequence[str]] = {},
    check_item: bool = True,
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
    # selective mode (15.7, hub contract 8.1/8.6): ``item_hash`` exactly in selective mode, only the four decisions on
    # a ticket log, text-only answers
    if ("item_hash" in event) != (mode == "selective") or (
        "item_hash" in event
        and (type(event["item_hash"]) is not str or _ITEM_HASH.fullmatch(event["item_hash"]) is None)
    ):
        raise HubRefused("malformed")
    if mode == "selective":
        if typ not in SELECTIVE_TYPES or log == "workspace":
            raise HubRefused("not_custodial")
        if typ == "question.answered" and "option" in event:
            raise HubRefused("malformed")
    if (typ, log_kind(log)) in CUSTODIAL_TYPES:  # the exact payload (8.5) before any certificate; others: step 7b
        _payload_shape(typ, event)
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
    role = wire.role_at(snapshot, user, now_ms)
    if mode == "selective":
        if user not in granted_users:
            raise HubRefused("not_granted")
        if check_item:  # admission only: shares are host state, so replay (``check_item=False``) cannot ask (15.7)
            item = live_items.get(event["item_hash"])
            if item is None and event["item_hash"] in superseded_items:
                raise HubRefused("event_changed")  # an earlier version of a live item: the person may sign again
            if type(item) is not dict or type(item.get("chain")) is not dict or type(item.get("f1")) is not dict:
                raise HubRefused("not_granted")
            _selective_item(event, log, item["chain"], item["f1"], log_heads)
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
    if (typ, log_kind(log)) not in CUSTODIAL_TYPES:
        _payload_shape(typ, event)  # unreachable for a type 7b let through; kept so no path skips the shape
    return {
        "user_id": user,
        "channel": event["channel"],
        "role": role,
        "floor": floor,
        "factor": event["channel_factor"],
    }


def _selective_item(
    event: Mapping[str, Any],
    log: str,
    chain: Mapping[str, Any],
    f1: Mapping[str, Any],
    log_heads: Mapping[str, Sequence[str]],
) -> None:
    """The item check at admission (ticket-format 15.7 steps 2-4). ``chain`` is the item's ``{log, seq, head}``, ``f1``
    its decision data. 2. the item is of this log and shows the decision's target (``gates[gate]``, ``verify`` for a
    verdict, ``questions[question]``) else ``not_granted``; 3. the event carries exactly the values the person was shown
    (``hash``, ``gate_gen``, ``policy_hash``, ``source_sha``, ``based_on`` = ``chain.head``, ``roster_v``) else
    ``event_changed``; 4. ``chain.head`` is the head of ``chain.log`` at ``chain.seq`` in the host's own log else
    ``view_fork``. Anything unreadable refuses."""
    if chain.get("log") != log:
        raise HubRefused("not_granted")
    typ = event["type"]
    if typ == "question.answered":
        targets, key = f1.get("questions"), event.get("question")
    else:
        targets, key = f1.get("gates"), "verify" if typ == "verdict.given" else event.get("gate")
    if type(targets) is not dict or type(key) is not str or key not in targets:
        raise HubRefused("not_granted")
    t = targets[key]
    if type(t) is not dict:
        raise HubRefused("not_granted")
    want = {"hash": t.get("hash"), "based_on": chain.get("head"), "roster_v": f1.get("roster_v")}
    if typ != "question.answered":
        want.update(gate_gen=t.get("gen"), policy_hash=t.get("policy_hash"))
    if "source_sha" in event:  # the payload shape requires it exactly on a code approval and a verdict
        want["source_sha"] = t.get("source_sha")
    if any(v is None or event.get(k) != v for k, v in want.items()):
        raise HubRefused("event_changed")
    heads, seq = log_heads.get(log), chain.get("seq")
    if (
        not isinstance(heads, Sequence)
        or type(seq) is not int
        or not 1 <= seq <= len(heads)
        or heads[seq - 1] != chain.get("head")
    ):
        raise HubRefused("view_fork")


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
    live_items: Mapping[str, Any] = {},
    superseded_items: frozenset[str] = frozenset(),
    log_heads: Mapping[str, Sequence[str]] = {},
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
        revoked_op_certs=revoked, live_items=live_items, superseded_items=superseded_items, log_heads=log_heads,
    )  # fmt: skip


# --- admission on the host (ticket-format 15.5) ----------------------------------------------------------------------

#: Replies after which the hub resends the same event bytes (15.5 "Which acks are final"); every other code is final.
RETRYABLE = frozenset({"unavailable", "unknown_snapshot"})
#: A snapshot pin is rate-limited per hub to one per this many ms of ``at``, unless it only narrows (15.2).
SNAPSHOT_PIN_INTERVAL_MS = 10 * 60 * 1000
#: The contract codes a model ``hub.invalid`` detail may already be (everything else is ``malformed``).
_CONTRACT_DETAILS = frozenset(
    {"malformed", "not_bound", "wrong_mode", "other_hub", "other_workspace", "not_joined", "unavailable"}
)


class Landing(Protocol):
    """What admission needs from the host's store (``orch.host.hubdeliver.StoreLanding`` is the real one). Every
    method runs inside :meth:`locked`, so the dedup check, the pre-check, the pins and the append see one state."""

    def locked(self) -> contextlib.AbstractContextManager[Any]: ...

    def core(self) -> Any:
        """The model's current ``Core`` (``ws`` and ``tickets``), read-only."""

    def logged(self, log: str, event_id: str) -> Mapping[str, Any] | None:
        """The line of ``log`` with this event id, or ``None``."""

    def refresh_snapshot(self) -> bool:
        """``snapshot_get`` over the link, verified and pinned in the hub state; ``True`` when the pin changed."""

    def last_snapshot_pin_ms(self, hub_id: str) -> int | None:
        """The ``at`` (ms) of the newest ``hub.pinned`` snapshot line of this hub in the workspace log."""

    def shared_items(self, log: str) -> tuple[Mapping[str, Any], frozenset[str]]:
        """Selective mode: the live shared items of this host as ``{item_hash: {"chain", "f1"}}`` and the hashes of
        their earlier versions (C6 ``ShareState``). Only the current versions are live."""

    def log_heads(self, log: str) -> Sequence[str]:
        """The heads of ``log`` in order (``heads[seq - 1]`` is the head at ``seq``), read lazily."""

    def land(self, pins: list[dict[str, Any]], event: dict[str, Any], log: str) -> None:
        """Judge ``pins`` then ``event`` in this order on a scratch state (``model.preview``); only when all admit,
        append them in this order under the same lock. Raises the model's :class:`Refusal` (in a ``LandRefused``)
        or ``StoreError``; a refusal appends nothing at all."""


class LandRefused(Exception):
    """The model refused the pins or the event; ``refusal`` is its :class:`~orch.model.Refusal`."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(f"{refusal.code.value}: {refusal.detail}")
        self.refusal = refusal


def reply_code(refusal: Refusal) -> str:
    """The ``event_deliver`` reply for a model refusal (ticket-format 15.5, hub contract 14)."""
    c = refusal.code
    if c in (Code.HUB_CERT_INVALID, Code.HUB_DENIED, Code.HUB_UNKNOWN_SNAPSHOT):
        return refusal.detail or "forbidden_role"
    if c == Code.HUB_FOREIGN_USER:
        return "foreign_user"
    if c in (Code.HUB_INVALID, Code.CHAIN_BROKEN) or c.value.startswith("validation."):
        return refusal.detail if c == Code.HUB_INVALID and refusal.detail in _CONTRACT_DETAILS else "malformed"
    if c in (Code.MEMBERS_STALE, Code.GATE_STALE, Code.QUESTION_BAD_HASH, Code.EVENT_BAD_BASE, Code.QUESTION_ANSWERED):
        return "event_changed"  # the view moved: the person may prepare and sign again
    if c == Code.EVENT_DUPLICATE_ID:
        return "replay"
    if c == Code.FREEZE_ACTIVE:
        return "unavailable"
    return "forbidden_role"


def _signed_part(line: Mapping[str, Any]) -> bytes:
    """``cj`` of the event without the host fields: what dedup compares (15.5 step 2)."""
    return cj({k: v for k, v in line.items() if k not in HOST_FIELDS})


def pins_needed(
    state: Any, ledger: Mapping[str, Any] | None, last_snapshot_ms: int | None, at_ms: int
) -> list[dict[str, Any]]:
    """The ledger pins (15.2) the host's hub state holds and the workspace log does not, in order: the card, then the
    op revocations, then the newest snapshot. A snapshot is pinned at most once per 10 minutes of ``at`` per hub,
    unless it lowers, removes or revokes anything (even if it also adds): then it always pins."""
    if ledger is None:
        return []
    out: list[dict[str, Any]] = []
    if state.card is not None and (ledger["card"] is None or cj(state.card) != cj(ledger["card"])):
        out.append(state.card)
    for cid in sorted(set(state.revoked_op_certs) - set(ledger["revoked_op_certs"])):
        obj = state.revoked_op_objs.get(cid)
        if obj is None:
            raise HubRefused("unavailable")  # a revocation we hold only as an id cannot be pinned: fail closed
        out.append(obj)
    snap, held = state.snapshot, ledger["snapshot"]
    if snap is not None and (held is None or snap["o"]["version"] > held["o"]["version"]):
        if (
            held is None
            or _lowers(snap["o"], held["o"])
            or last_snapshot_ms is None
            or (at_ms - last_snapshot_ms >= SNAPSHOT_PIN_INTERVAL_MS)
        ):
            out.append(snap)
    return out


def _lowers(new: Mapping[str, Any], old: Mapping[str, Any]) -> bool:
    """Does the new snapshot take anything away: a user removed, a role lowered or a channel newly revoked? Whatever
    else it adds does not matter: a demotion must reach the model at once (a snapshot that also adds a user is not
    "wider")."""
    rank = {r: i for i, r in enumerate(ROLES)}
    o_roles, n_roles = old["roles"], new["roles"]
    o_ends, n_ends = old["ends_ms"], new["ends_ms"]
    return (
        bool(set(new["revoked_channels"]) - set(old["revoked_channels"]))
        or any(u not in n_roles or rank[n_roles[u]] < rank[r] for u, r in o_roles.items())
        or any(u not in o_ends or t < o_ends[u] for u, t in n_ends.items())  # an end date added or earlier (GA-07)
    )


def _selective_args(state: Any, ws: Any, land: Landing, log: str) -> dict[str, Any]:
    """Selective mode (D80, 15.7): the granted users are the aliases of the owner-signed ``hub.joined``; each one's F1
    role is that of its canonical person (L2P4-1); the live items and the log's heads come from the host's own state."""
    book = (ws.hub or {}).get("alias_book", {}).get(state.hub_id, {}) if ws.hub else {}
    live, superseded = land.shared_items(log)
    return {
        "granted_users": frozenset(book.values()),
        "member_roles": {u: r for p, u in book.items() if (r := role_of(ws, "p_" + p)) is not None},
        "live_items": live,
        "superseded_items": superseded,
        "log_heads": {log: land.log_heads(log)},
    }


def accept_event_deliver(
    state: Any,
    body: Any,
    *,
    land: Landing,
    now_ms: int,
    dev_mode: bool,
) -> dict[str, Any]:
    """The host's answer to an h2w ``event_deliver`` (protocol 10.2, ticket-format 15.5): ``body`` is ``{"log",
    "event"}``. Under the store lock, in order: shape and binding; **dedup by event id** (same signed part: done, other
    bytes: ``replay``) BEFORE any verification; the contract verification as a fast pre-check, with the
    ``min_factor`` of ``policies.factor_floor`` (never a literal) and one ``snapshot_get`` on ``unknown_snapshot``;
    the ledger pins the event needs; then ``land`` (the model's admit at the new position, and one append of the pins
    and the event, nothing on a refusal). Returns the verified facts, or the dedup marker ``{"duplicate": True}``.
    Raises :class:`HubRefused` with the reply code (final unless in :data:`RETRYABLE`)."""
    if type(body) is not dict or set(body) != {"log", "event"} or type(body["log"]) is not str:
        raise HubRefused("malformed")
    if state is None or state.status != "bound":
        raise HubRefused("not_bound")
    if state.mode not in ("full", "selective"):
        raise HubRefused("wrong_mode")
    log, event = body["log"], body["event"]
    if type(event) is not dict or type(event.get("id")) is not str or type(event.get("type")) is not str:
        raise HubRefused("malformed")

    def attempt() -> dict[str, Any]:
        with land.locked():
            existing = land.logged(log, event["id"])
            if existing is not None:
                if _signed_part(existing) == _signed_part(event):
                    return {"duplicate": True}
                raise HubRefused("replay")
            core = land.core()
            floor = factor_floor(core.ws, core.tickets.get(log), event["type"], event.get("gate"))
            kw = _selective_args(state, core.ws, land, log) if state.mode == "selective" else {}
            verified = verify_hub_event(state, event, log=log, now_ms=now_ms, dev_mode=dev_mode, min_factor=floor, **kw)
            led = ledger_of(core.ws, state.hub_id)
            pins = [
                {"type": "hub.pinned", "actor": {"kind": "host"}, "hub_id": state.hub_id, "object": obj}
                for obj in pins_needed(state, led, land.last_snapshot_pin_ms(state.hub_id), now_ms)
            ]
            land.land(pins, event, log)
            return verified

    try:
        try:
            return attempt()
        except HubRefused as e:
            # one snapshot_get, OUTSIDE the store lock (a slow hub must not hold up the workspace), then once more
            if e.code != "unknown_snapshot" or not land.refresh_snapshot():
                raise
            return attempt()
    except LandRefused as e:
        code = reply_code(e.refusal)
        # The wire code is coarse (most model refusals read "forbidden_role"): the host log keeps the real reason
        # (FB15 round 3: an add-on request was dropped with no way to tell why).
        logging.getLogger("orch.hubclient.custodial").warning(
            "event_deliver %s refused: reply %s, model %s (%s)",
            event.get("type"), code, e.refusal.code.value, str(e.refusal.detail)[:200],
        )  # fmt: skip
        raise HubRefused(code) from None
