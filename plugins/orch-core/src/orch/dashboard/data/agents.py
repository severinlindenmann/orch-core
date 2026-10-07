from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from orch.clock import now as clock_now
from orch.clock import parse_stamp
from orch.core import events as events_mod
from orch.core import query, store
from orch.dashboard.data.tasks import waiting_on_human

STATUS_LABELS = {"working": "Working", "waiting": "Waits for you", "stale": "Stale"}

_STATUS_ORDER = {"waiting": 0, "stale": 1, "working": 2}


@dataclass(frozen=True)
class AgentRow:
    ticket: str
    title: str
    harness: str          # claim["harness"], e.g. "claude-code"
    session: str           # claim["session"], shown shortened to 8 chars in the template
    status: str            # "working" | "waiting" | "stale"
    claimed_at: datetime | None
    last_at: datetime | None


def _num(ticket_id: str) -> int:
    m = re.search(r"(\d+)$", ticket_id)
    return int(m.group(1)) if m else 0


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return parse_stamp(str(stamp))
    except ValueError:
        return None


def _harness_name(actor: str) -> str:
    """"agent:<name>[:session]" -> "<name>"; anything else is returned unchanged."""
    if actor.startswith("agent:"):
        return actor[len("agent:"):].split(":", 1)[0]
    return actor


def _claimed_entries(ws, entries: list[store.Entry] | None = None) -> list[store.Entry]:
    """Tickets an agent holds right now (a claim with a session); odd frontmatter counts as unclaimed."""
    out = []
    for e in store.scan(ws) if entries is None else entries:
        if e.meta is None:
            continue
        claim = e.meta.get("claim")
        if isinstance(claim, dict) and claim.get("session"):
            out.append(e)
    return out


def agent_rows(ws, *, now: datetime | None = None, events: list | None = None,
               entries: list[store.Entry] | None = None, needs: list[dict] | None = None) -> list[AgentRow]:
    """One row per claimed ticket. `events`, `entries` (a `store.scan`) and `needs` (a
    `query.needs_you`) may be shared with the rest of the request; each is computed once if not given."""
    at_now = now or clock_now()
    entries = store.scan(ws) if entries is None else entries
    if needs is None:
        needs = query.needs_you(ws, entries=entries)
    waiting_ids = waiting_on_human(needs, entries)
    all_events = events if events is not None else events_mod.read_events(ws)
    by_ticket: dict[str | None, list] = {}
    for ev in all_events:
        by_ticket.setdefault(ev.ticket, []).append(ev)
    rows: list[AgentRow] = []
    for e in _claimed_entries(ws, entries):
        claim = e.meta["claim"]
        claimed_at = _parse(claim.get("at"))
        last_at = query.claim_last_at(claim, by_ticket.get(e.id, []))  # the same rule as query.stale_claims
        if e.id.upper() in waiting_ids:
            status = "waiting"
        elif query.is_silent(ws, last_at, at_now):
            status = "stale"
        else:
            status = "working"
        rows.append(AgentRow(
            ticket=e.id,
            title=e.meta.get("title") or "",
            harness=str(claim.get("harness") or ""),
            session=events_mod.short_session(claim.get("session")) or "",
            status=status,
            claimed_at=claimed_at,
            last_at=last_at,
        ))
    return sorted(rows, key=lambda r: (_STATUS_ORDER[r.status], _num(r.ticket)))


def recent_sessions(ws, *, limit: int = 10, events: list | None = None) -> list[dict]:
    all_events = events if events is not None else events_mod.read_events(ws)
    # One pass in log order: the latest claim.taken per (ticket, session) seen so far is the one
    # a later claim.released of that pair closes.
    latest_taken: dict[tuple, object] = {}
    sessions: list[dict] = []
    for ev in sorted(all_events, key=lambda e: e.seq):
        if ev.kind == "claim.taken":
            latest_taken[(ev.ticket, events_mod.short_session(ev.data.get("session")))] = ev
            continue
        if ev.kind != "claim.released":
            continue
        rel, rel_session = ev, events_mod.short_session(ev.data.get("session"))
        taken_ev = latest_taken.get((rel.ticket, rel_session))
        if taken_ev is None:
            continue
        started, ended = _parse(taken_ev.at), _parse(rel.at)
        if started is None or ended is None:
            continue
        minutes = max(0, int((ended - started).total_seconds() // 60))
        sessions.append({
            "ticket": rel.ticket,
            "harness": _harness_name(taken_ev.actor),
            "session": rel_session,
            "started": started,
            "ended": ended,
            "minutes": minutes,
        })
    sessions.sort(key=lambda s: s["ended"], reverse=True)
    return sessions[:limit]
