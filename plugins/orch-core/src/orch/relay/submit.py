"""R2: the durable remote-submission adapter (contract 11A.3 "The exception", 11A.4 ``relay.submit``, 11A.12).

* **The closed event/scope table** (:data:`EVENTS`): ``new_ticket`` -> ``ticket.created`` (scope ``operate``),
  ``answer`` -> ``question.answered`` (scope ``decide``). Any other event type is ``forbidden_scope``. It is the
  ``event`` column of :data:`orch.relay.framing.OPS`, checked to agree by a test.
* :func:`check_events` (the lock-free checks): exactly one F1 person event per request, of the table's type, its id
  equal to the idem and fresh (its ULID time in [now - 900 s, now + 300 s]), whose actor is exactly the requesting
  device and its person, whose ``auth`` is in :data:`AUTH`, without ``source_sha`` or any host-stamped field, whose
  ``sig`` verifies under **this** device's ``dk_sig_pub`` over ``person_signing_bytes(ws, log, E)``; ``log`` is a
  ticket uid. Every failure is a sealed refusal; nothing is appended. The store verifies every signature again.
* :func:`recover`: start-up roll-forward of admitted writes (11A.4 "admitted" row): no outcome record, or ``pending``
  that reconciles to nothing landed: run again from the submission row (identity rechecked; no new seq admission, no
  ts window); ``pending`` with landed events: rolled forward through ``relay_fields`` (never judged again); terminal:
  nothing to run. The bridge then answers every admitted write from ``outcome.get``.
* :func:`make_relay_fields`: the ``OutcomeStore.relay_fields`` hook R5 registers at open.

``orch.host`` is import-banned here: the host side is reached through :class:`orch.relay.bridge.HostPort` (the host
adapter ``orch/host/relayentry.py`` implements it with ``Call(service, ...)`` and ``append_relay``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from orch import canon, crypto
from orch.canon.hashing import HashError

from .framing import FRESH_FUTURE_MS, FRESH_PAST_MS, OPS, Identity, Refusal, ulid_ms
from .rids import RidStore

__all__ = ["AUTH", "EVENTS", "check_events", "make_relay_fields", "recover", "result"]

log = logging.getLogger("orch.relay.submit")

#: the closed event/scope table (11A.4): relay op -> (event type, scope)
EVENTS: Mapping[str, tuple[str, str]] = {
    "new_ticket": ("ticket.created", "operate"),
    "answer": ("question.answered", "decide"),
}
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_HOST_FIELDS = frozenset({"seq", "at", "prev", "ws_seq", "host_sig", "source_sha"})


#: the device auth kinds column R accepts (orch-52 final, 15 Oct 2026); convenience and every other kind refused
AUTH = frozenset({"secure-enclave-unlocked", "passphrase"})


def check_events(
    op: str, args: Mapping[str, Any], identity: Identity, dk_sig_pub: bytes, *, idem: str, now_ms: int
) -> tuple[str, list[dict]]:
    """The lock-free checks of a ``relay.submit`` request, in order: the args shape and exactly one event
    (``invalid.input``), the event type of the closed table (``forbidden_scope``), no host-stamped field
    (``invalid.input``), idem == event id (``idem.invalid``), the event id's ULID freshness (``event.expired``), the
    actor (``op.not_allowed``), the auth kind (``op.not_allowed``), the device signature (``op.not_allowed``).
    Returns ``(log, events)``."""
    table = EVENTS.get(op)
    if table is None:
        raise Refusal("forbidden_scope")
    if set(args) != {"log", "events"} or type(args["events"]) is not list or type(args["log"]) is not str:
        raise Refusal("invalid.input", retryable=False)
    log_id, events = args["log"], args["events"]
    if not _ULID.fullmatch(log_id) or len(events) != 1 or type(events[0]) is not dict:
        raise Refusal("invalid.input", retryable=False)  # "exactly one event" is a validation code
    ev = events[0]
    if ev.get("type") != table[0]:
        raise Refusal("forbidden_scope")
    if _HOST_FIELDS & set(ev) or type(ev.get("id")) is not str or not _ULID.fullmatch(ev["id"]):
        raise Refusal("invalid.input", retryable=False)
    if idem != ev["id"]:
        raise Refusal("idem.invalid", retryable=False)
    born = ulid_ms(ev["id"])
    if not now_ms - FRESH_PAST_MS <= born <= now_ms + FRESH_FUTURE_MS:
        raise Refusal("event.expired", retryable=False)
    if ev.get("actor") != {"kind": "person", "id": identity.person_id, "device": identity.device_id}:
        raise Refusal("op.not_allowed", retryable=False)
    if ev.get("auth") not in AUTH:  # convenience never through the relay (11A.6)
        raise Refusal("op.not_allowed", retryable=False)
    try:
        sig = crypto.unb64u(ev.get("sig"), crypto.SIG_LEN)
        signed = canon.person_signing_bytes(identity.workspace_id, log_id, ev)
    except (crypto.EncodingError, HashError, canon.JcsError, TypeError, ValueError):
        raise Refusal("op.not_allowed", retryable=False) from None
    if not crypto.verify(dk_sig_pub, sig, signed):
        raise Refusal("op.not_allowed", retryable=False)
    return log_id, [dict(ev)]


class SubmitPort(Protocol):
    """What :func:`recover` needs from the host (a subset of :class:`orch.relay.bridge.HostPort`); called on the
    workspace's worker thread."""

    def submit(
        self, identity: Identity, idem: str, op: str, log: str, events: list[dict], recheck: Callable[[], bool]
    ) -> dict[str, Any]: ...

    def lookup(self, identity: Identity, idem: str) -> dict[str, Any]: ...


def recover(
    port: SubmitPort,
    rids: RidStore,
    identity_of: Callable[[str, int], tuple[Identity, Callable[[], bool]] | None],
) -> list[tuple[str, str, str]]:
    """Roll every admitted write forward so its outcome is terminal (or stays pending where the log cannot say).
    ``identity_of(device, epoch)`` rebuilds the identity and its recheck from R6 and the host (``None``: the device
    no longer holds; the write is then refused ``op.not_allowed`` by the run, never appended). Returns
    ``(device, rid, what)`` per write; idempotent (a second run finds the outcomes terminal)."""
    out = []
    for rec in rids.admitted():
        if rec.kind != "write" or rec.idem is None:
            continue
        sub = rids.submission(rec.device, rec.rid)
        built = identity_of(rec.device, rec.epoch)
        if sub is None or built is None:
            out.append((rec.device, rec.rid, "unanswerable"))
            continue
        identity, recheck = built
        try:
            state = port.lookup(identity, rec.idem).get("state")
        except Refusal as r:
            state = None if r.code == "outcome.unknown_idem" else "error"
        if state in ("committed", "failed", "partial"):
            out.append((rec.device, rec.rid, "terminal"))
            continue
        if state == "error":
            out.append((rec.device, rec.rid, "unknown"))
            continue
        op, log_id, events = sub
        try:  # no record, or a pending one: begin() reconciles it (landed: relay_fields; nothing: runs again)
            port.submit(identity, rec.idem, op, log_id, events, recheck)
            what = "ran"
        except Refusal as r:
            what = f"refused:{r.code}"
        out.append((rec.device, rec.rid, what))
        log.info("relay.submit recover device=%s rid=%s %s", rec.device, rec.rid, what)
    return out


Append = Callable[[str, list[dict], list[dict], str, str], tuple[list[int], Any]]


def make_relay_fields(append: Append) -> Callable[[dict[str, Any]], dict[str, Any] | str]:
    """The ``relay_fields`` hook (H12a): a pending ``relay.submit`` record some of whose events landed is rolled
    forward with the remaining events of its own record (``append(log, events, extras, person, device)``: R5 binds it
    to ``append_relay``), never judged again. Returns the terminal fields, or ``"keep"`` when the record cannot say."""

    def relay_fields(record: dict[str, Any]) -> dict[str, Any] | str:
        args = record.get("args_normalised") or {}
        events, log_id = record.get("signed_events"), args.get("log")
        principal = record.get("principal")
        if type(events) is not list or not events or type(log_id) is not str or type(principal) is not str:
            return "keep"
        actor = events[0].get("actor") if type(events[0]) is dict else None
        if type(actor) is not dict or principal != f"r:{actor.get('device')}":
            return "keep"
        landed, error = append(log_id, events, [{} for _ in events], actor["id"], actor["device"])
        if error is None and len(landed) == len(events):
            result = _result(log_id, landed)
            return {"step": "committed", "result": result, "exit": 0}
        err = error.error() if error is not None else {"code": "internal", "message": "not every event landed"}
        return {"step": "failed", "error": err, "exit": getattr_exit(error), "seqs": [int(s) for s in landed]}

    return relay_fields


def getattr_exit(error: Any) -> int:
    exit_code = error.exit if error is not None else 1
    return exit_code if type(exit_code) is int else 1


def _result(log_id: str, seqs: Sequence[int]) -> dict[str, Any]:
    """The §0.3 Result of a committed ``relay.submit``: ``data {log, seqs}``, ``seq`` the last one."""
    return {
        "data": {"log": log_id, "seqs": [int(s) for s in seqs]},
        "key": None,
        "seq": int(max(seqs)) if seqs else 0,
        "cursor": 0,
        "hints": [],
        "lines": [],
        "duplicate": False,
        "exit": 0,
        "head": None,
    }


def result(log_id: str, seqs: Sequence[int], *, duplicate: bool = False) -> dict[str, Any]:
    return {**_result(log_id, seqs), "duplicate": duplicate}


assert {k for k, v in OPS.items() if v.kind == "write"} == set(EVENTS)
assert all(OPS[k].event == t and OPS[k].scope == s for k, (t, s) in EVENTS.items())
