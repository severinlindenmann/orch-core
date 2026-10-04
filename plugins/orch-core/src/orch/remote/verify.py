"""Apply a decision from a paired phone as the human, only when core can prove it came from that phone
(spec §6.4). Core does every check here, never the addon: the pairing, the HMAC (constant time, over canonical
JSON), the age, the per-kind permission, the ledger (no replay, one decision per target) and the target hash. The
change itself runs through `intents.execute` (a ticket request: `Ops.new`) with `Actor("human", "you",
"phone:<label>", device=<phone id>)`, so it gets every check a desktop click gets and the signed approval ledger
names the phone. R21: a paired phone is the owner, so a decision that passes every check applies at once, with no
second step on the desktop. Anything that is not proven comes back `pending` and never applies by itself: a
revoked or unknown phone, a bad signature, an implausible time or a kind the owner switched off."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from orch.core.canonical import canonical_json
from orch.errors import NotFoundError, OrchError, TransitionError, UsageError, ValidationError
from orch.remote import ledger
from orch.remote import store as phones

MAX_AGE_DAYS = 14
MAX_SKEW_S = 300
# What a phone may decide directly. Never move, close, reopen or import (P0 ruling: no move from a phone). A
# ticket request only creates a backlog ticket, as the desktop's New ticket does.
DIRECT_KINDS = ("answer", "request_changes", "approve", "verdict", "ticket_request")
STATUSES = ("applied", "pending", "stale", "superseded", "answered-locally", "duplicate", "unlinked")
# Why a decision was not applied (or that it was): the stable, machine-readable `RemoteResult.code`. Messages are
# human text and may be reworded; these strings never change meaning. Documented in ADDONS.md (API 2.4).
CODES = ("applied", "malformed", "kind-not-allowed", "not-paired", "bad-signature", "implausible-time", "too-old",
         "kind-switched-off", "no-such-ticket", "question-not-found", "already-handled", "superseded",
         "already-approved", "answered-locally", "changed-since", "wrong-status", "wrong-round",
         "refused-retry", "refused-final")
_DEC_ID = re.compile(r"dec_[0-9a-f]{32}")
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9 ._-]")
_EVENT_KIND = {"answer": "question.answered", "approve": "gate.approved",
               "request_changes": "gate.changes_requested", "verdict": "verdict.given",
               "ticket_request": "ticket.created"}


@dataclass(frozen=True)
class RemoteResult:
    status: str
    message: str
    ticket: str | None = None
    event_seq: int | None = None
    code: str = ""  # one of CODES; "" only for a result an addon built itself


def mac_of(key: bytes, decision: dict) -> str:
    """b64url (no padding) HMAC-SHA256 of the canonical JSON of `decision` without its "mac"."""
    body = canonical_json({k: v for k, v in decision.items() if k != "mac"})
    return base64.urlsafe_b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode("ascii").rstrip("=")


def _at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def _pending(message: str, code: str, ticket: str | None = None) -> RemoteResult:
    return RemoteResult("pending", message, ticket, code=code)


def _target_problem(kind: str, target: dict) -> str | None:
    from orch.core.gates import GATE_SECTIONS
    if kind == "answer":
        if not (isinstance(target.get("qid"), str) and isinstance(target.get("hash"), str) and target["hash"]):
            return "an answer needs the question id and the hash of the question the phone showed"
    elif kind in ("approve", "request_changes"):
        if target.get("gate") not in GATE_SECTIONS or not (isinstance(target.get("hash"), str) and target["hash"]):
            return "a gate decision needs the gate and the hash of the text the phone showed"
        if "plan_hash" in target and not (kind == "approve" and target.get("gate") == "requirements"
                                          and isinstance(target["plan_hash"], str) and target["plan_hash"]):
            return "requirements and plan together need the requirements gate and the plan's hash"
    elif not (target.get("status") == "testing" and isinstance(target.get("round"), int)
              and not isinstance(target.get("round"), bool) and target["round"] >= 0):
        return "a verdict needs status testing and the testing round it was given in"
    elif not (isinstance(target.get("hash"), str) and target["hash"]):
        # fail closed: a phone app that does not send the document's verdict hash yet decides nothing
        return ("a verdict needs the verdict hash of the criteria and evidence the phone showed; update the phone app "
                "(until then, give the verdict on the desktop)")
    return None


def verify_and_apply(ws, decision: dict, *, addon: str, now: datetime | None = None) -> RemoteResult:
    from orch.clock import now as clock_now
    from orch.core import store

    now = now or clock_now()
    if not isinstance(decision, dict) or decision.get("v") != 1 or not isinstance(decision.get("decision_id"), str) \
            or not _DEC_ID.fullmatch(decision["decision_id"]):
        return _pending("not a version 1 decision", "malformed")
    kind, target = decision.get("kind"), decision.get("target")
    if kind not in DIRECT_KINDS:
        return _pending("a phone cannot decide this kind of thing; do it on the desktop", "kind-not-allowed")
    if kind == "ticket_request":
        problem = _request_problem(decision)
        if problem:
            return _pending(problem, "malformed")
        target = {}
    elif not isinstance(target, dict) or any(decision.get(k) is not None and not isinstance(decision.get(k), str)
                                             for k in ("value", "note", "ticket", "pair")):
        return _pending("the decision is malformed", "malformed")
    phone = phones.find(ws.root, decision.get("pair") or "")
    if phone is None or phone.revoked_at or phone.addon != addon:
        return _pending("this phone is not paired with this workspace", "not-paired")
    given = decision.get("mac")
    try:
        expected = mac_of(phone.key, decision).encode("ascii")
    except (TypeError, ValueError):
        return _pending("the phone's signature did not verify", "bad-signature")
    if not isinstance(given, str) or not hmac.compare_digest(given.encode("utf-8"), expected):
        return _pending("the phone's signature did not verify", "bad-signature")
    at = _at(decision.get("at"))
    if at is None or at > now + timedelta(seconds=MAX_SKEW_S):
        return _pending("the decision's time is not plausible; apply it on the desktop if it is right",
                        "implausible-time")
    if now - at > timedelta(days=MAX_AGE_DAYS):
        return _ledgered_stale(ws, decision, kind, phone)
    if not phones.permissions(ws.root).get(kind):
        return _pending(f"{kind.replace('_', ' ')} from the phone is switched off (Workspace → Phones); "
                        "do it on the desktop", "kind-switched-off")
    if kind == "ticket_request":
        with ledger.lock(ws):
            return _apply_request(ws, decision, phone)
    problem = _target_problem(kind, target)
    if problem:
        return _pending(problem, "malformed")
    try:
        tid = store.resolve(ws, decision.get("ticket") or "").id
    except (NotFoundError, UsageError):
        return RemoteResult("unlinked", "no such ticket in this workspace", code="no-such-ticket")
    with ledger.lock(ws):
        return _apply(ws, decision, kind, target, tid, phone)


def _request_problem(decision: dict) -> str | None:
    """A ticket request names no ticket and no target; its value is {title, body} (and an optional voice note)."""
    from orch.addons.api import MAX_ASK_TEXT, MAX_NEW_TITLE
    value = decision.get("value")
    if decision.get("ticket") is not None or decision.get("target") not in (None, {}):
        return "a ticket request names no ticket"
    if not isinstance(decision.get("pair"), str) or (decision.get("voice") is not None
                                                     and not isinstance(decision.get("voice"), dict)):
        return "the decision is malformed"
    if not isinstance(value, dict) or not isinstance(value.get("title"), str) \
            or not isinstance(value.get("body", ""), str):
        return "a ticket request needs a title (and an optional body)"
    title = " ".join(value["title"].split())
    if not title or len(title) > MAX_NEW_TITLE:
        return f"a ticket request's title is 1 to {MAX_NEW_TITLE} characters"
    if len(value.get("body", "")) > MAX_ASK_TEXT:
        return "the ticket request's text is too long"
    return None


def _apply_request(ws, decision, phone) -> RemoteResult:
    """A backlog ticket from a signed ticket request, as the desktop's New ticket would create it."""
    from orch.core.events import read_events
    from orch.core.model import neutral_text
    from orch.core.ops import Ops
    from orch.core.events import Actor

    did = decision["decision_id"]
    if ledger.seen(ws, did):
        return RemoteResult("duplicate", "already handled", code="already-handled")
    title = " ".join(decision["value"]["title"].split())
    body = neutral_text(str(decision["value"].get("body") or "").strip())
    label = _LABEL_SAFE.sub("", phone.label)[:40] or "phone"
    actor = Actor("human", "you", f"phone:{label}", device=phone.id)
    try:
        t = Ops(ws, actor).new(title, ask=body)
    except OrchError as e:
        return _pending(e.message, "refused-retry")
    from orch.actor import process_evidence
    from orch.core import ledger as signed_ledger
    try:  # signed like every other phone decision: which decision, which phone
        signed_ledger.record(ws, ticket=t.id, kind="ticket_request", actor=actor, evidence=process_evidence(),
                             decision=did)
    except OrchError as e:  # the ticket exists; `orch check` reports the missing signature
        message = f"created {t.id} from {label}, but {e.message}"
    else:
        message = f"created {t.id} from {label}"
    written = [e for e in read_events(ws, t.id) if e.via == actor.via]
    seq = next((e.seq for e in written if e.kind == "ticket.created"), None)
    return _record(ws, did, t.id, f"request:{did}", "ticket_request", phone,
                   RemoteResult("applied", message, t.id, seq, "applied"),
                   event_seqs=[e.seq for e in written])


def _ledgered_stale(ws, decision, kind, phone) -> RemoteResult:
    result = RemoteResult("stale", f"older than {MAX_AGE_DAYS} days; never applied automatically", code="too-old")
    with ledger.lock(ws):
        if ledger.seen(ws, decision["decision_id"]):
            return RemoteResult("duplicate", "already handled", code="already-handled")
        return _record(ws, decision["decision_id"], None, None, kind, phone, result)


def _apply(ws, decision, kind, target, tid, phone) -> RemoteResult:
    from orch.addons import intents
    from orch.addons.api import Intent
    from orch.core import store
    from orch.core.events import Actor, read_events, latest_testing_round
    from orch.core.gates import gate_hash, gate_state
    from orch.core.model import neutral_text
    from orch.core.questions import find_question, question_hash

    did = decision["decision_id"]
    if ledger.seen(ws, did):
        return RemoteResult("duplicate", "already handled", tid, code="already-handled")
    t = store.load(ws, tid)[1]
    ticket_events = read_events(ws, tid)
    if kind == "answer":
        key = f"q:{target['qid'].strip().upper()}"
    elif kind == "verdict":  # one verdict per testing round: a follow-up that reopens testing is a new round
        round_now = latest_testing_round(ticket_events)
        key = f"verdict:{round_now}"
    else:  # one gate decision per gate text: changed text is a new target
        key = f"gate:{target['gate']}:{target['hash']}"

    shown = target.get("hash") if isinstance(target.get("hash"), str) else None

    def done(result: RemoteResult) -> RemoteResult:
        return _record(ws, did, tid, key, kind, phone, result, shown=shown)

    if ledger.decided(ws, tid, key):
        return done(RemoteResult("superseded", "another phone decision was applied first", tid, code="superseded"))
    value = neutral_text(decision.get("value") or "")
    note = neutral_text(decision.get("note") or "")
    try:
        if kind == "answer":
            try:
                q = find_question(t, target["qid"])
            except NotFoundError as e:
                return _pending(e.message, "question-not-found", tid)
            if q.get("answer") not in (None, ""):
                return done(RemoteResult("answered-locally", "answered on the desktop", tid, code="answered-locally"))
            if question_hash(q) != target["hash"]:
                return done(RemoteResult("stale", "the question changed since", tid, code="changed-since"))
            intent, anchor = Intent("answer", ref=tid, qid=q["id"], value=value, reason=note,
                                    expected_hash=target["hash"]), q["id"]
        elif kind == "approve":
            gate = target["gate"]
            if gate_hash(t, gate) != target["hash"]:
                return done(RemoteResult("stale", f"the {gate} changed since", tid, code="changed-since"))
            if gate_state(t, gate) == "approved":
                return done(RemoteResult("superseded", "already approved", tid, code="already-approved"))
            if target.get("plan_hash"):  # F2: requirements and plan in one decision, each bound to its own hash
                if gate_hash(t, "plan") != target["plan_hash"]:
                    return done(RemoteResult("stale", "the plan changed since", tid, code="changed-since"))
                return _apply_together(ws, did, tid, key, phone, target, ticket_events, shown)
            intent, anchor = Intent("approve", ref=tid, gate=gate, expected_hash=target["hash"]), f"gate:{gate}"
        elif kind == "request_changes":
            gate = target["gate"]
            if gate_hash(t, gate) != target["hash"]:
                return done(RemoteResult("stale", f"the {gate} changed since", tid, code="changed-since"))
            intent, anchor = Intent("request_changes", ref=tid, gate=gate, reason=value or note,
                                    expected_hash=target["hash"]), f"gate:{gate}"
        else:
            if t.status != "testing":
                return done(RemoteResult("stale", f"{tid} is {t.status}, not testing", tid, code="wrong-status"))
            if target.get("round") != round_now:
                return done(RemoteResult("stale", "this verdict was signed for an earlier testing round", tid,
                                        code="wrong-round"))
            from orch.core.epics import verdict_hash
            if verdict_hash([t], ws) != target["hash"]:
                return done(RemoteResult("stale", "the criteria or evidence changed since", tid, code="changed-since"))
            intent, anchor = Intent("verdict", ref=tid, value=value, reason=note,
                                    expected_hash=target["hash"]), "verdict"
    except ValueError as e:  # an Intent field over its cap
        return _pending(str(e), "malformed", tid)

    label = _LABEL_SAFE.sub("", phone.label)[:40] or "phone"
    actor = Actor("human", "you", f"phone:{label}", device=phone.id)
    last = max((e.seq for e in ticket_events), default=0)
    try:
        intents.execute(ws, intent, allowed_ref=tid, tickets=False, actor=actor, source="resolve",
                        decisions=True, anchor=anchor)
    except (ValidationError, TransitionError) as e:
        if kind == "answer" and _answered(ws, tid, intent.qid):
            return done(RemoteResult("answered-locally", "answered on the desktop", tid, code="answered-locally"))
        return done(RemoteResult("stale", e.message, tid, code="refused-final"))
    except OrchError as e:
        return _pending(e.message, "refused-retry", tid)
    written = [e for e in read_events(ws, tid, after=last) if e.via == actor.via]
    seq = next((e.seq for e in written if e.kind == _EVENT_KIND[kind]), None)
    applied = RemoteResult("applied", f"applied from {label}", tid, seq, "applied")
    return _record(ws, did, tid, key, kind, phone, applied,
                   event_seqs=[e.seq for e in written], shown=shown)


def _apply_together(ws, did, tid, key, phone, target, ticket_events, shown) -> RemoteResult:
    """Requirements and plan approved together (Ops.approve_together: every check a desktop confirm gets)."""
    from orch.core.events import Actor, read_events
    from orch.core.ops import Ops
    label = _LABEL_SAFE.sub("", phone.label)[:40] or "phone"
    actor = Actor("human", "you", f"phone:{label}", device=phone.id)
    last = max((e.seq for e in ticket_events), default=0)
    try:
        Ops(ws, actor).approve_together(tid, requirements_hash=target["hash"], plan_hash=target["plan_hash"])
    except (ValidationError, TransitionError) as e:
        refused = RemoteResult("stale", e.message, tid, code="refused-final")
        return _record(ws, did, tid, key, "approve", phone, refused, shown=shown)
    except OrchError as e:
        return _pending(e.message, "refused-retry", tid)
    written = [e for e in read_events(ws, tid, after=last) if e.via == actor.via]
    seq = next((e.seq for e in written if e.kind == "gate.approved"), None)
    applied = RemoteResult("applied", f"applied from {label}", tid, seq, "applied")
    return _record(ws, did, tid, key, "approve", phone, applied,
                   event_seqs=[e.seq for e in written], shown=shown)


def _answered(ws, tid: str, qid: str) -> bool:
    from orch.core import store
    from orch.core.questions import find_question
    try:
        return find_question(store.load(ws, tid)[1], qid).get("answer") not in (None, "")
    except OrchError:
        return False


def _record(ws, did, tid, key, kind, phone, result: RemoteResult, *, event_seqs=(), shown=None) -> RemoteResult:
    """Ledger every outcome except pending (which may still apply later: a retry once the phone is paired again
    or the kind is switched back on, or the same decision made on the desktop).
    `shown`: the hash of what the phone showed (question, gate text or verdict hash)."""
    from orch.clock import stamp_s
    ledger.append(ws, {"decision_id": did, "ticket": tid, "target": key, "kind": kind, "phone": phone.id,
                       "status": result.status, "event_seq": result.event_seq, "event_seqs": list(event_seqs),
                       "hash": shown, "at": stamp_s()})
    return result
