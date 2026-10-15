"""R2: the durable remote-submission adapter (contract 11A.3 "The exception", 11A.4 ``relay.submit``, 11A.12).

* **The closed event/scope table** (:data:`EVENTS`): ``new_ticket`` -> ``ticket.created`` and ``answer`` ->
  ``question.answered``, both scope ``decide`` (11A.14 route scope, L2, the F1 model and the 14.8 phone profile
  ``scopes_max: ["look","decide"]``; review I1). Any other event type is ``forbidden_scope``. It is the
  ``event`` column of :data:`orch.relay.framing.OPS`, checked to agree by a test.
* :func:`check_events` (the lock-free checks of 14.6, P1 to P7, with 14.6's codes): P1 exactly one event, ``log`` a
  ticket uid, its id equal to the idem (``invalid.input``); P2 the table's type (``forbidden_scope``); P3 the
  submitted-subset schema (no host-stamped field or ``source_sha``, ``v`` 2, ``hash_v`` 1, a creation's ``based_on``
  null, the F1 event schema and the §11.3 text rules: ``validation.event`` / ``validation.text``); P4 the actor is
  exactly the requesting device and its person, ``sig`` present; P5 ``auth`` in :data:`AUTH` (``op.not_allowed``);
  P7 ``sig`` verifies under **this** device's ``dk_sig_pub`` over ``person_signing_bytes(ws, log, E)``
  (``sig.invalid``). Every failure is a definite sealed refusal (D); nothing is appended. The store verifies every
  signature again. **No freshness check here** (orch-52 ruling A): the event id's ULID window ([host now - 900 s,
  host now + 300 s], ``event.expired``) is checked by the host INSIDE the store-lock span, after the outcome record
  and the log dedup answered every known identity, so a committed event resent late replays its outcome instead of
  being refused.
* :func:`recover`: start-up roll-forward of admitted writes (11A.4 "admitted" row): no outcome record, or ``pending``
  that reconciles to nothing landed: run again from the submission row (identity AND the op's scope rechecked; no new
  seq admission, no ts window); ``pending`` with landed events: rolled forward through ``relay_fields`` (never judged
  again); terminal: nothing to run. The bridge then answers every admitted write from ``outcome.get``.
  :func:`recover_one` is the same for one rid (the bridge's bounded retry of a write whose run failed).
* :func:`make_relay_fields`: the ``OutcomeStore.relay_fields`` hook R5 registers at open.

``orch.host`` is import-banned here: the host side is reached through :class:`orch.relay.bridge.HostPort` (the host
adapter ``orch/host/relayentry.py`` implements it with ``Call(service, ...)`` and ``append_relay``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from orch import canon, crypto, schema
from orch.canon.hashing import HashError

from .framing import OPS, Identity, Refusal
from .rids import Rid, RidStore

__all__ = ["AUTH", "EVENTS", "check_events", "check_schema", "make_relay_fields", "recover", "recover_one", "result"]

log = logging.getLogger("orch.relay.submit")

#: the closed event/scope table (11A.4): relay op -> (event type, scope)
EVENTS: Mapping[str, tuple[str, str]] = {
    "new_ticket": ("ticket.created", "decide"),
    "answer": ("question.answered", "decide"),
}
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_HOST_FIELDS = frozenset({"seq", "at", "prev", "ws_seq", "host_sig", "source_sha"})
#: P3: the host stamp a submitted event is probed with (never sent, never stored): the schema checks the rest
_PROBE_AT, _PROBE_SIG, _PROBE_HEAD = "2026-01-01T00:00:00Z", "A" * 86, "sha256:" + "0" * 64


#: the device auth kinds column R accepts (orch-52 final, 15 Oct 2026); convenience and every other kind refused
AUTH = frozenset({"secure-enclave-unlocked", "passphrase"})


def check_events(
    op: str, args: Mapping[str, Any], identity: Identity, dk_sig_pub: bytes, *, idem: str
) -> tuple[str, list[dict]]:
    """The lock-free checks of a ``relay.submit`` request (14.6 P1 to P7, in order, with 14.6's codes): P1 the args
    shape, exactly one event, ``log`` a ticket uid, idem == event id (``invalid.input``); P2 the event type of the
    closed table (``forbidden_scope``); P3 the submitted-subset schema (``validation.event`` /
    ``validation.text``); P4 the actor (``op.not_allowed``); P5 the auth kind (``op.not_allowed``); P7 the device
    signature (``sig.invalid``). Returns ``(log, events)``. Freshness is the host's, after dedup (ruling A)."""
    table = EVENTS.get(op)
    if table is None:
        raise Refusal("forbidden_scope")
    if set(args) != {"log", "events"} or type(args["events"]) is not list or type(args["log"]) is not str:
        raise Refusal("invalid.input")
    log_id, events = args["log"], args["events"]
    if not _ULID.fullmatch(log_id) or len(events) != 1 or type(events[0]) is not dict:
        raise Refusal("invalid.input")  # "exactly one event"
    ev = events[0]
    if ev.get("id") != idem:
        raise Refusal("invalid.input")  # P1: opts.idem == events[0].id (14.6; review M2)
    if ev.get("type") != table[0]:
        raise Refusal("forbidden_scope")
    check_schema(ev)
    if ev.get("actor") != {"kind": "person", "id": identity.person_id, "device": identity.device_id}:
        raise Refusal("op.not_allowed")
    if ev.get("auth") not in AUTH:  # convenience never through the relay (11A.6)
        raise Refusal("op.not_allowed")
    try:
        sig = crypto.unb64u(ev.get("sig"), crypto.SIG_LEN)
    except (crypto.EncodingError, TypeError, ValueError):
        raise Refusal("op.not_allowed") from None  # P4: sig present (and of its encoding)
    try:
        signed = canon.person_signing_bytes(identity.workspace_id, log_id, ev)
    except (HashError, canon.JcsError, TypeError, ValueError):
        raise Refusal("validation.event") from None
    if not crypto.verify(dk_sig_pub, sig, signed):
        raise Refusal("sig.invalid")
    return log_id, [dict(ev)]


def check_schema(ev: Mapping[str, Any]) -> None:
    """14.6 P3, the submitted-subset schema: no host-stamped field (``seq``, ``at``, ``prev``, ``ws_seq``,
    ``host_sig``) and no ``source_sha``; the id a ULID; ``v`` 2, ``hash_v`` 1; a creation's ``based_on`` null; then
    the F1 event schema (exact field set, ``cj``-canonical, the type's own rules) on the event with a probe stamp, and
    the §11.3 text rules (``validation.text``). Every failure is a definite refusal before the store lock, so an
    unjudgeable event never reaches it (review I3)."""
    if _HOST_FIELDS & set(ev) or type(ev.get("id")) is not str or not _ULID.fullmatch(ev["id"]):
        raise Refusal("validation.event")
    creation = ev.get("type") == "ticket.created"
    if ev.get("v") != 2 or ev.get("hash_v") != 1 or (creation and ev.get("based_on") is not None):
        raise Refusal("validation.event")
    probe = {
        **ev, "seq": 1 if creation else 2, "prev": None if creation else _PROBE_HEAD, "at": _PROBE_AT, "ws_seq": 1,
        "host_sig": _PROBE_SIG,
    }  # fmt: skip
    try:
        schema.validate("event", probe, log="ticket")
    except schema.SchemaError as e:
        raise Refusal("validation.text" if e.message.startswith("text rule") else "validation.event") from None
    except (TypeError, ValueError, KeyError):
        raise Refusal("validation.event") from None


class SubmitPort(Protocol):
    """What :func:`recover` needs from the host (a subset of :class:`orch.relay.bridge.HostPort`); called on the
    workspace's worker thread."""

    def submit(
        self, identity: Identity, idem: str, op: str, log: str, events: list[dict], recheck: Callable[[], bool]
    ) -> dict[str, Any]: ...

    def lookup(self, identity: Identity, idem: str) -> dict[str, Any]: ...


IdentityOf = Callable[[str, int, str | None], tuple[Identity, Callable[[], bool]] | None]


def recover(port: SubmitPort, rids: RidStore, identity_of: IdentityOf) -> list[tuple[str, str, str]]:
    """Roll every admitted write forward so its outcome is terminal (or stays pending where the log cannot say).
    ``identity_of(device, epoch, scope)`` rebuilds the identity and its recheck (steps 1 to 3 AND ``scope``, the op's)
    from R6 and the host (``None``: the device no longer holds; the write is then never run). Returns
    ``(device, rid, what)`` per write; idempotent (a second run finds the outcomes terminal)."""
    out = []
    for rec in rids.admitted():
        if rec.kind == "write" and rec.idem is not None:
            out.append((rec.device, rec.rid, recover_one(port, rids, identity_of, rec)))
    return out


def recover_one(port: SubmitPort, rids: RidStore, identity_of: IdentityOf, rec: Rid) -> str:
    """:func:`recover` for one admitted write: ``terminal``, ``unknown`` (the outcome store could not say),
    ``unanswerable`` (no submission row, or the device or the op's scope no longer holds), ``ran`` or
    ``refused:<code>``. Raises what ``port.submit`` raises when that is not a refusal."""
    sub = rids.submission(rec.device, rec.rid)
    if sub is None or rec.idem is None:
        return "unanswerable"
    op, log_id, events = sub
    spec = OPS.get(op)
    built = identity_of(rec.device, rec.epoch, spec.scope if spec is not None else "-")
    if built is None or spec is None:
        return "unanswerable"
    identity, recheck = built
    try:
        state = port.lookup(identity, rec.idem).get("state")
    except Refusal as r:
        state = None if r.code == "outcome.unknown_idem" else "error"
    if state in ("committed", "failed", "partial"):
        return "terminal"
    if state == "error":
        return "unknown"
    try:  # no record, or a pending one: begin() reconciles it (landed: relay_fields; nothing: runs again)
        port.submit(identity, rec.idem, op, log_id, events, recheck)
        what = "ran"
    except Refusal as r:
        what = f"refused:{r.code}"
    log.info("relay.submit recover device=%s rid=%s %s", rec.device, rec.rid, what)
    return what


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
        return {"step": "failed", "error": err, "exit": _exit_of(error), "seqs": [int(s) for s in landed]}

    return relay_fields


def _exit_of(error: Any) -> int:
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
