"""The core helpers of ``relay.submit`` admission (orch-v2-host §11A.14, H12e): what R2's handler
(``orch/relay/submit.py``) calls for the steps of 14.6 that read core state. R2 owns the order, the outcome plan and the
classification; nothing here appends, plans or records anything.

* P5: :data:`COLUMN_R_AUTH`, the closed ``auth`` set of column R (R2 refuses others with ``op.not_allowed``).
* L3 then the window (amendment A): :func:`known_or_fresh`, a known identity first, then :func:`check_window` (the
  ULID time in [now - 900 s, now + 300 s]: ``event.expired``) only for a never-seen event.
* L3: :func:`dedup`, by :func:`signed_digest` and ``actor``, never by signature bytes (:func:`lookup` is the raw
  read): an identical landed event returns its ``seq``; ``event.duplicate_id``, ``event.abandoned``.
* L4, L5: :func:`check_target`, the ticket exists and is visible, or the uid is unused; the owner; the exact next
  key: ``not_found``, ``ticket.exists``, ``invalid.input``, ``ticket.key_not_next``.
* L5: :func:`next_key`, ``Store.peek_key`` (``_next_key()`` after the directory scan, under the lock).
* L6: :func:`orch.host.human.judge_events` (re-exported here): the model's code.

The L helpers must run inside **one** ``store.locked()`` span that R2 opens after ``store.refresh()`` and holds through
``append_relay`` and the outcome record (the lock is re-entrant; every helper takes it again, so a helper called
alone is still consistent with itself). They take the submitted form of 14.1: the event without ``seq``, ``at``,
``prev``, ``ws_seq`` and ``host_sig``, with ``sig``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from orch import canon

from .errors import HostError
from .human import judge_events

__all__ = [
    "COLUMN_R_AUTH",
    "WINDOW_FUTURE_S",
    "WINDOW_PAST_S",
    "Logged",
    "check_target",
    "check_window",
    "dedup",
    "device_standing",
    "judge_events",
    "known_or_fresh",
    "lookup",
    "next_key",
    "signed_digest",
    "ulid_ms",
]

#: P5: the only ``auth`` values column R accepts (14.3); ``convenience`` is never one of them
COLUMN_R_AUTH = frozenset({"secure-enclave-unlocked", "passphrase"})
#: step W (was P6; amendment A: only after L3): an id's ULID time must lie in [host now − 900 s, host now + 300 s]
WINDOW_PAST_S = 900
WINDOW_FUTURE_S = 300

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_CHARS = frozenset(_CROCKFORD)


def ulid_ms(event_id: str) -> int:
    """The 48-bit millisecond time of a ULID (``^[0-7][0-9A-HJKMNP-TV-Z]{25}$``); ``validation.event`` otherwise."""
    if (
        not isinstance(event_id, str)
        or len(event_id) != 26
        or event_id[0] not in "01234567"
        or not set(event_id) <= _ULID_CHARS
    ):
        raise HostError("validation.event", "the event id is not a ULID")
    n = 0
    for ch in event_id[:10]:
        n = n * 32 + _CROCKFORD.index(ch)
    return n


def device_standing(store: Any, person_id: str, device_id: str) -> Literal["ok", "removed", "unknown"]:
    """The liveness of a device in the F1 log (11A.4 step 3: "neither removed nor revoked"), for column R's
    ``standing()`` in R2: **the** API it must use (never ``state._core``, never ``device_person`` alone, which also
    answers for a removed device).

    * ``"ok"``: ``device_id`` was added (``device.added``) for ``person_id`` and is neither removed (``device.removed``,
      or its person's ``member.removed``) nor revoked (``device.revoked``).
    * ``"removed"``: it is known for ``person_id`` and removed or revoked, or a revocation of it by ``person_id`` is on
      record (a tombstone).
    * ``"unknown"``: no such device of this person.

    Certificate expiry is not part of it (``verify_cert`` covers that). Takes the lock itself (re-entrant), so it is
    safe to call inside R2's ``store.locked()`` span; every entry into the store lock re-checks freshness, so it sees
    what is on disk now.

    **A removal is not durable across a restore.** A restore that abandons a ``device.removed`` revives the device
    (existing F1 behaviour, bounded by the 11A.4 hold-off and ``readers reset``): the standing is ``"ok"`` again. Only
    a revocation survives a restore (it is put back by the ``restore`` event), so a **lost phone must be REVOKED**,
    not just removed."""
    from orch.store.store import device_standing_ok

    with store.locked():
        state = store.state
        view = state.workspace.devices.get(device_id)
        if view is not None:
            if view.person != person_id:
                return "unknown"
            # R6a round 5: the "ok" decision is THE one standing function the relay gate uses (no second copy)
            return "ok" if device_standing_ok(state, device_id, person_id) else "removed"
        if (device_id, person_id) in state.revocation_evidence():
            return "removed"
        return "unknown"


def check_window(event_id: str, now: float) -> None:
    """Step W (14.4): ``event.expired`` unless the id's ULID time is within [``now`` − 900 s, ``now`` + 300 s], both
    edges included, in milliseconds. ``now`` is the host clock at job time (epoch seconds). Replay and readers never
    call this: only admission does."""
    t = ulid_ms(event_id)
    now_ms = int(now * 1000)
    if not now_ms - WINDOW_PAST_S * 1000 <= t <= now_ms + WINDOW_FUTURE_S * 1000:
        raise HostError(
            "event.expired",
            f"the event id's time is outside the admission window [now - {WINDOW_PAST_S} s, now + {WINDOW_FUTURE_S} s]",
        )


def _check_log(log: Any, *, workspace: bool) -> None:
    """``log`` is a ticket uid (a ULID), or ``"workspace"`` where ``workspace`` allows it; never a key or a number
    (``Store.ticket`` resolves those): ``validation.event``, so no ``StoreError`` leaks out of a helper."""
    if log == "workspace" and workspace:
        return
    if not isinstance(log, str) or len(log) != 26 or log[0] not in "01234567" or not set(log) <= _ULID_CHARS:
        raise HostError("validation.event", "the log is not a ticket uid")


def signed_digest(workspace_id: str, log: str, event: dict[str, Any]) -> str:
    """``"sha256:" + hex(sha256(person_signing_bytes(workspace_id, log, event)))``: the digest of the signed content
    (14.4). ``sig`` and the host stamp (``seq``, ``at``, ``prev``, ``ws_seq``, ``host_sig``) are not part of it, so a
    randomized or high-s signature of the same content has the same digest. The outcome plan records it (L7)."""
    return "sha256:" + hashlib.sha256(canon.person_signing_bytes(workspace_id, log, event)).hexdigest()


@dataclass(frozen=True)
class Logged:
    """An event of the log with the looked-up id: its ``seq``, the :func:`signed_digest` of its content, its actor."""

    seq: int
    digest: str
    actor: dict[str, Any]


def lookup(store: Any, log: str, event_id: str) -> Logged | Literal["abandoned"] | None:
    """The raw L3 read, under the lock: the logged event with this id in ``log`` (:class:`Logged`), ``"abandoned"``
    for an id a restore abandoned (taken in the replayed log, F1 §5.10, but not in its lines), or ``None``. A log
    that does not exist yet (a creation's fresh uid) has no ids."""
    _check_log(log, workspace=True)
    with store.locked():
        if log != "workspace" and store.ticket(log, heal=False) is None:
            return None
        found = store.logged_event(log, event_id)
        if found is not None:
            ws = store.state.workspace.workspace_id
            return Logged(int(found["seq"]), signed_digest(ws, log, found), found.get("actor"))
        lc = store.state._core.logs.get(log)  # the replay core's log: ids include the abandoned decisions
        if lc is not None and event_id in lc.ids:
            return "abandoned"
        return None


def dedup(store: Any, log: str, event: dict[str, Any]) -> int | None:
    """L3 (14.4): dedup by event identity. Returns ``None`` for an id this log has never taken, or the ``seq`` of
    the logged event when it has the same :func:`signed_digest` **and** the same ``actor`` (R2 answers
    ``committed``, ``duplicate:true``, with that ``seq``, and stops). Raises ``event.duplicate_id`` for the id logged
    with other content or another actor, and ``event.abandoned`` for an id a restore abandoned."""
    _check_log(log, workspace=True)
    with store.locked():
        found = lookup(store, log, event["id"])
        if found is None:
            return None
        if found == "abandoned":
            raise HostError("event.abandoned", "a restore abandoned the event with this id; review again")
        ws = store.state.workspace.workspace_id
        if found.digest == signed_digest(ws, log, event) and found.actor == event.get("actor"):
            return found.seq
        raise HostError("event.duplicate_id", "an event with this id and other content is in the log")


def known_or_fresh(store: Any, log: str, event: dict[str, Any], now: float) -> int | None:
    """L3 then the window, in that order (amendment A of 15 Oct 2026): a known identity is answered **before** the
    freshness check, so freshness never turns a committed write into a re-review. Returns the ``seq`` of an identical
    landed event (``committed``, ``duplicate:true``); raises :func:`dedup`'s codes for a known id; only a never-seen
    event gets :func:`check_window` (``event.expired``) and then ``None``. R2 answers the outcome store by
    ``(ws, "r:<device>", idem = id)`` before it calls this."""
    _check_log(log, workspace=True)
    with store.locked():
        seq = dedup(store, log, event)
        if seq is not None:
            return seq
        check_window(event["id"], now)
        return None


def next_key(store: Any) -> str:
    """The key the store allocates next (``_next_key()`` after the directory scan, under the lock). Advisory outside
    the admission span; inside it (after ``refresh``) it is the one key a creation may carry (L5)."""
    return store.peek_key()


def check_target(store: Any, log: str, event: dict[str, Any]) -> None:
    """L4 and L5 (14.6), under the lock, for a submitted ``question.answered`` or ``ticket.created`` of ``log``.

    * ``log`` must be a ticket uid: a key, a number or ``"workspace"`` is ``validation.event`` (neither relay type
      belongs on the workspace log).
    * Any type but ``ticket.created``: the ticket exists and the actor's person may see it, else ``not_found`` (one
      message for missing and invisible).
    * ``ticket.created``: the uid is unused (``ticket.exists``); ``owner`` is the actor's person (``invalid.input``);
      the signer must be a member who is not a viewer, on a device whose standing is ``ok`` with ``decide`` (else
      ``role.denied``, before the uid or any key is compared, so the advisory ``next_key`` never reaches a
      principal that may not create: the rule of ``reads_ws.may_create``);
      ``key`` is exactly :func:`next_key` (``ticket.key_not_next``, with ``detail = {"next_key": ...}``: advisory,
      never a reservation; exit 3, not retryable). A taken key is never the next key, so a key race is always
      ``ticket.key_not_next``, never ``ticket.exists``."""
    from orch.ops.runtime import visible_to

    _check_log(log, workspace=False)  # neither relay type belongs on the workspace log: refused here (fail closed)
    with store.locked():
        actor = event.get("actor") or {}
        view = store.ticket(log, heal=False)
        if event.get("type") != "ticket.created":
            if view is None or not visible_to(actor.get("id"), view):
                raise HostError("not_found", "no such ticket")
            return
        ws = store.state.workspace
        member = ws.members.get(actor.get("id"))
        dev = ws.devices.get(actor.get("device"))
        if (
            member is None
            or member.role == "viewer"
            or device_standing(store, str(actor.get("id")), str(actor.get("device"))) != "ok"
            or (dev is not None and "decide" not in dev.scopes)
        ):
            # who may not create learns nothing about the key counter (M1, #499): the model's own code, no detail
            raise HostError("role.denied", "this signer may not create tickets")
        if view is not None:
            raise HostError("ticket.exists", "a ticket with this uid exists")
        if event.get("owner") != actor.get("id"):
            raise HostError("invalid.input", "a person's ticket.created names the signer as its owner")
        want = next_key(store)
        if event.get("key") != want:
            raise HostError(
                "ticket.key_not_next",
                f"{event.get('key')} is not the next key; the next key is {want} (advisory)",
                detail={"next_key": want},
            )
