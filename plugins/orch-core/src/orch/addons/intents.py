"""Core executes what an addon's `resolve()` / `act()` asks for (ruling R-A1-INTENT). Addon code never holds an `Ops`:
it returns an `Intent`, this module checks it against what the human clicked on and runs it with the human actor."""
from __future__ import annotations

from orch.addons.api import MAX_ASK_TEXT, MAX_NEW_TITLE, TICKET_INTENTS, Intent
from orch.errors import UsageError, ValidationError

MAX_MESSAGE = 300


def as_intent(result) -> Intent:
    """`None` and a plain string mean "nothing to change" (the string is the message); anything else must be an Intent."""
    if result is None:
        return Intent("none")
    if isinstance(result, str):
        return Intent("none", reason=result[:MAX_MESSAGE])
    if isinstance(result, Intent):
        return result
    raise ValidationError(f"the addon returned {type(result).__name__}, not an Intent")


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.strip().upper() == b.strip().upper()


def execute(ws, intent: Intent, *, allowed_ref: str | None, tickets: bool, actor, source: str,
            decisions: bool = False, anchor: str | None = None) -> str:
    """Validate and run `intent` as `actor` (the dashboard's human). `allowed_ref` is the decision's ticket or the
    action's target: an intent about any other ticket is refused. `tickets` is the action's `"tickets": true`.
    `source` is `"act"` or `"resolve"`: an action may only return `none`, or close/reopen/import when declared with
    `"tickets": true`; answer, approve, request_changes, verdict, move and new come only from a decision's
    `resolve()`, where the human saw the item they decide on. `decisions` says the addon declares the `decisions`
    capability, which `new` needs. Answer, approve and verdict must carry the hash of what the human saw; a stale hash is
    refused and nothing is written. `anchor` is the decision's PendingDecision.anchor: a decision drawn on question
    Q<n> may only answer Q<n>."""
    from orch.core.ops import Ops

    if source not in ("act", "resolve"):
        raise ValueError(f"unknown intent source {source!r}")
    kind = intent.kind
    if kind == "none":
        return intent.reason
    if source == "act" and kind not in TICKET_INTENTS:
        raise ValidationError(f"an action may not return a {kind} intent; that comes only from a decision; "
                              "nothing changed")
    if source == "resolve":
        tickets = False  # decisions never get close/reopen/import
    if kind == "new":
        return _new(ws, actor, intent, decisions=decisions)
    if not _same(intent.ref, allowed_ref):
        raise ValidationError(f"the addon asked to change {intent.ref or 'no ticket'}, "
                              f"but this item is about {allowed_ref or 'no ticket'}; nothing changed")
    if kind in TICKET_INTENTS and not tickets:
        raise ValidationError(f"{kind} is only allowed for actions declared with \"tickets\": true")
    ops = Ops(ws, actor)
    ref = intent.ref.strip()
    if kind == "answer":
        if not intent.qid or intent.value is None:
            raise ValidationError("an answer intent needs qid and value")
        if not intent.expected_hash:
            raise ValidationError("an answer intent needs expected_hash (the question hash the human saw)")
        if anchor and anchor.startswith("Q") and not _same(intent.qid, anchor):
            raise ValidationError(f"this item is drawn on {anchor}, but the addon answered {intent.qid}; "
                                  "nothing changed")
        t = ops.answer(ref, intent.qid, intent.value, note=intent.reason or None, expected_hash=intent.expected_hash)
        return f"Answered {intent.qid} on {t.id}"
    if kind == "approve":
        if not intent.gate or not intent.expected_hash:
            raise ValidationError("an approve intent needs gate and expected_hash (the gate text the human saw)")
        t = ops.approve(ref, intent.gate, expected_hash=intent.expected_hash)
        return f"Approved the {intent.gate} of {t.id}"
    if kind == "request_changes":
        if not intent.gate:
            raise ValidationError("a request_changes intent needs gate and reason")
        # expected_hash is optional here: without one (None or "") the request is sent without the stale check,
        # because asking for changes never approves anything; with one, a changed gate text refuses it.
        t = ops.request_changes(ref, intent.gate, intent.reason, expected_hash=intent.expected_hash or None)
        return f"Asked for changes on the {intent.gate} of {t.id}"
    if kind == "verdict":
        if not intent.expected_hash:
            raise ValidationError("a verdict intent needs expected_hash (the verdict hash of the criteria and evidence "
                                  "the human saw); update the addon or the phone app. Nothing changed")
        t = ops.verdict(ref, intent.value or "", intent.reason or None, expected_hash=intent.expected_hash)
        return f"Verdict {intent.value} on {t.id}"
    if kind == "move":
        t = ops.move(ref, intent.value or "")
        return f"Moved {t.id} to {intent.value}"
    if kind == "import":
        title = " ".join((intent.value or "").split())
        if not title:
            raise ValidationError("an import intent needs the title in value")
        ask = intent.data.get("ask")
        ask = ask[:MAX_ASK_TEXT] if isinstance(ask, str) else ""
        kind_of = {k: v for k in ("type", "priority") if isinstance((v := intent.data.get(k)), str) and v}
        return f"Imported {ref.upper()} as {_import(ws, ops, ref, title, intent.reason, ask, **kind_of)}"
    if kind == "close":
        t = ops.close(ref, intent.reason)
        return f"Closed {t.id}"
    if kind == "reopen":
        t = ops.reopen(ref, intent.reason)
        return f"Reopened {t.id} to {t.status}"
    raise UsageError(f"{kind} intents are not supported by this orch-core")


def _new(ws, actor, intent: Intent, *, decisions: bool) -> str:
    """A backlog ticket from a decision: no ref, the title in value (capped), the Ask in reason (already capped)."""
    from orch.core.model import neutral_text
    from orch.core.ops import Ops

    if not decisions:
        raise ValidationError("a new intent comes only from an addon with the decisions capability; nothing changed")
    if intent.ref is not None:
        raise ValidationError("a new intent is about no ticket yet; leave ref empty. Nothing changed")
    title = " ".join((intent.value or "").split())
    if not title:
        raise ValidationError("a new intent needs the title in value")
    if len(title) > MAX_NEW_TITLE:
        raise ValidationError(f"a new ticket's title is at most {MAX_NEW_TITLE} characters")
    t = Ops(ws, actor).new(title, ask=neutral_text(intent.reason.strip()))
    return f"Created {t.id} in backlog"


def _import(ws, ops, key: str, title: str, reason: str = "", ask: str = "", type: str | None = None,
            priority: str | None = None) -> str:
    """The ticket for an external key; an existing one if a ticket already has that key (idempotent). `reason`
    (e.g. "Imported from <url>") is logged on a newly created ticket, never on one that already existed. `ask` (the
    issue's own text) becomes the new ticket's Ask through `neutral_text`, so it can never forge a section. `type` and
    `priority` (from the issue's labels) apply when they are ones orch knows; anything else keeps the default, and
    an epic is never made by an import."""
    from orch.core import store
    from orch.core.constants import PRIORITIES, TYPES
    from orch.core.model import neutral_text

    wanted = key.strip().upper()
    for entry in store.scan(ws):
        for x in (entry.meta or {}).get("external") or []:
            if isinstance(x, dict) and str(x.get("key", "")).upper() == wanted:
                return entry.id
    chosen = {}
    if type in TYPES and type != "epic":
        chosen["type"] = type
    if priority in PRIORITIES:
        chosen["priority"] = priority
    ticket = ops.new(title, external=wanted, ask=neutral_text(ask.strip()), **chosen)
    if reason:
        ops.log(ticket.id, reason)
    return ticket.id
