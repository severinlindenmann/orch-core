"""Tell the hub what agents did in this workspace (hub contract 10.10, ``agent_events``; HUB-AC-03 #1426).

The workspace decides what leaves: for every log line whose actor is an agent, FOUR references and nothing else: the
event type word, the time, the agent session name and the ticket key. Never a title, comment, prompt, path or secret.
The hub has no op to ask for more. Sent on a hub link use, like the display status; a failure is logged without
content and the position stays, so it is retried on the next use. The position is kept in memory per hub and starts
at the current end of every log, so connecting never uploads history. Needs no hub: nothing runs without a link."""

from __future__ import annotations

import calendar
import logging
import re
import time
from typing import Any

from orch.store.store import WORKSPACE

__all__ = ["MAX_EVENTS", "collect", "send"]

LOG = logging.getLogger("orch.host.agentevents")
MAX_EVENTS = 50
#: the closed set the hub accepts (hub contract 10.10); any other event type is reported as "event"
KINDS = frozenset(
    {
        "artifact.added",
        "artifact.replaced",
        "branch.pushed",
        "event",
        "followup.cancelled",
        "followup.fired",
        "followup.outcome",
        "followup.rescheduled",
        "followup.restored",
        "followup.set",
        "gate.approved",
        "gate.changes_requested",
        "gate.invalidated",
        "grant.issued",
        "grant.requested",
        "grant.revoked",
        "handoff.written",
        "question.answered",
        "question.asked",
        "status.changed",
        "ticket.closed",
        "ticket.created",
        "ticket.moved",
        "ticket.reopened",
        "ticket.submitted",
        "ticket.updated",
        "verdict.given",
    }
)
_AGENT = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:@-]*")
_TICKET = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")


def _ts_ms(stamp: Any) -> int:
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) * 1000
    except (TypeError, ValueError):
        return 0


def _ticket_key(store: Any, log: str, cache: dict[str, str]) -> str:
    if log == WORKSPACE:
        return ""
    if log not in cache:
        first = store.events_after(log, 0, 1)
        key = first[0][0].get("key") if first else None
        cache[log] = key if isinstance(key, str) and _TICKET.fullmatch(key) else ""
    return cache[log]


def collect(store: Any, position: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``(events, new_position)``: at most :data:`MAX_EVENTS` agent lines after ``position`` (``{log: last seq}``). The
    first call (empty ``position``, no ``"init"``) only records where every log ends."""
    marks = [k for k in [WORKSPACE, *sorted(store.log_marks())] if k]
    seen = dict(position.get("logs", {}))
    if not position.get("init"):
        start = {log: store.log_seq(log) for log in dict.fromkeys(marks)}
        position["unread"] = {log for log, n in start.items() if n is None}
        return [], {log: n for log, n in start.items() if n is not None}  # unreadable now: starts at its end later
    keys: dict[str, str] = position.setdefault("keys", {})
    out: list[dict[str, Any]] = []
    for log in dict.fromkeys(marks):
        head = store.log_seq(log)
        if head is None:
            continue
        if log not in seen and log in position.get("unread", ()):
            seen[log] = head  # it was unreadable on the first pass: its history is never uploaded
            continue
        after = seen.get(log, 0)
        if head <= after:
            continue
        for ev, _h in store.events_after(log, after, 256):
            if len(out) >= MAX_EVENTS:
                break
            after = int(ev["seq"])
            actor = ev.get("actor")
            if not isinstance(actor, dict) or actor.get("kind") != "agent":
                continue
            name = actor.get("session") or actor.get("id")
            kind = ev.get("type")
            out.append(
                {
                    "kind": kind if kind in KINDS else "event",
                    "ts_ms": _ts_ms(ev.get("at")),
                    "agent": name if isinstance(name, str) and len(name) <= 64 and _AGENT.fullmatch(name) else "agent",
                    "ticket": _ticket_key(store, log, keys),
                }
            )
        seen[log] = after
    return out, seen


def send(service: Any, state: Any, hub_link: Any) -> None:
    """Best effort, never raises. ``state`` is the hub binding (``hub_id``)."""
    try:
        if getattr(state, "mode", None) != "full":  # selective: never reveal unshared tickets (D80)
            service.__dict__.get("_agent_events_pos", {}).pop(state.hub_id, None)  # a later full pass starts at the end
            return
        store = getattr(service, "store", None)
        if store is None:
            return
        pos = service.__dict__.setdefault("_agent_events_pos", {})
        mine = pos.setdefault(state.hub_id, {"init": False, "logs": {}})
        events, new = collect(store, mine)
        if events:
            hub_link.call("agent_events", dict(events=events))  # noqa: C408 - a bare key: not a store path
        mine["init"], mine["logs"] = True, new
    except Exception as exc:  # noqa: BLE001 - display only: logged without content, retried on the next link use
        LOG.warning(
            "agent events not delivered (%s); will retry on the next hub link", getattr(exc, "code", type(exc).__name__)
        )
