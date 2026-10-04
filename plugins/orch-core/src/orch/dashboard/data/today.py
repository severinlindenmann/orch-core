"""Today (spec §4.3): the summary strip and the In flight cards. Pure helpers over what the
route already computed once per request (scan, needs_you, events, agent rows)."""
from __future__ import annotations

from datetime import datetime

from orch.core import store, tasks_view
from orch.dashboard.data import tasks as tasks_data
from orch.dashboard.data.agents import STATUS_LABELS as AGENT_LABELS
from orch.dashboard.data.steps import steps as step_list
from orch.errors import TicketParseError

IN_FLIGHT = ("in-progress", "testing", "open", "waiting")  # also the card order
AGENT_ROLES = {"working": "info", "waiting": "you", "stale": "warn"}


def summary(ws, *, needs, rows, now: datetime | None = None, decisions=None) -> dict:
    """{waiting, backlog, later, tickets, oldest, oldest_minutes, working, silent}. `needs` are query.waiting() items:
    `waiting` is their blocking count (query.counts, the same number as the tab title, the menu badge and the
    SessionStart hook), `backlog` and `later` the other scopes, `tickets` the distinct tickets with a blocking item,
    `oldest` the oldest blocking Decision of `decisions` (None without one that has an age) and `oldest_minutes` its
    age, `working`/`silent` the agent rows with status "working"/"stale"."""
    from orch.core import query
    n = query.counts(needs)
    blocking = [d for d in decisions or [] if d.scope == "blocking" and d.age_minutes is not None]
    oldest = max(blocking, key=lambda d: d.age_minutes, default=None)
    backlog = [d for d in decisions or [] if d.scope == "backlog" and d.age_minutes is not None]
    return {
        "waiting": n["blocking"],
        "backlog": n["backlog"],
        "later": n["later"],
        "tickets": len(query.blocking_ids(needs)),
        "oldest": oldest,
        "oldest_minutes": oldest.age_minutes if oldest else None,
        "backlog_oldest_minutes": max((d.age_minutes for d in backlog), default=None),
        "working": sum(1 for r in rows if r.status == "working"),
        "silent": sum(1 for r in rows if r.status == "stale"),
    }


def ready_backlog(summary: dict, n: int) -> dict:
    """The backlog line's count without the combined approvals Today shows under "Ready to start" (the headline, the
    badge and the tab title stay query.waiting's blocking count)."""
    return {**summary, "backlog": max(0, summary["backlog"] - n)} if n else summary


def split(decisions) -> dict[str, list]:
    """Decisions by scope, each in list order: {"blocking", "later", "backlog"}."""
    out: dict[str, list] = {"blocking": [], "later": [], "backlog": []}
    for d in decisions:
        out.get(d.scope, out["blocking"]).append(d)
    return out


def is_combined(d) -> bool:
    """M: requirements whose plan the agent drafted too (Decision.together): the Board's `move` reads "Approve
    requirements + plan" for it. Nobody claims the ticket until it is approved, so query.waiting scopes it as backlog;
    Today still lists it among the decisions (Today4), both full texts side by side."""
    return d.kind == "approve-requirements" and d.together is not None and d.scope == "backlog" and not d.charter


def split_today(decisions) -> dict[str, list]:
    """`split` for Today: the combined requirements + plan approvals leave the backlog line for their own section,
    "ready" ("Ready to start", below the decisions). They are not counted with the decisions: nothing blocks on
    them, and the headline, the badge, the tab title and the SessionStart hook all show query.waiting's blocking
    items. query.waiting itself is unchanged."""
    parts = split(decisions)
    parts["ready"] = [d for d in parts["backlog"] if is_combined(d)]
    parts["backlog"] = [d for d in parts["backlog"] if not is_combined(d)]
    return parts


def groups(decisions) -> list[dict]:
    """Decisions as [{"key", "label", "decisions"}] for the template: one entry per distinct `Decision.group`, in the
    order its first decision comes, so the list order is kept inside each group. Ungrouped decisions (group None,
    today every one) form a single entry without a heading. This is the hook epic grouping plugs into."""
    out: list[dict] = []
    at: dict = {}
    for d in decisions:
        if d.group not in at:
            at[d.group] = len(out)
            out.append({"key": d.group, "label": getattr(d, "group_label", None) or d.group, "decisions": []})
        out[at[d.group]]["decisions"].append(d)
    return out


FULL_CARDS = 5  # per section, the first decisions drawn as full cards; the rest are one-line rows (#17: page weight)
STALE_ROWS = 10  # silent claims listed on Today; the rest are on Activity
FLIGHT_CARDS = 8  # In flight cards on Today (most urgent status first); the rest are on the Board


def layout(decisions) -> dict:
    """How Today draws a section's decisions: {"full": ids drawn as full cards, "stale": stale-claim decisions,
    "decisions": the rest in list order}. The first FULL_CARDS non-stale decisions are full cards (each with its
    full gated text before any Approve); the others are one-line rows that open the one-by-one focus view, so a page
    with 100 waiting decisions stays light. Silent claims gather in one compact list."""
    stale = [d for d in decisions if d.kind == "stale-claim"]
    rest = [d for d in decisions if d.kind != "stale-claim"]
    return {"full": {d.cid for d in rest[:FULL_CARDS]}, "stale": stale, "decisions": rest}


def neighbours(decisions, at: str | None) -> tuple[int, object | None, str | None, str | None]:
    """For the one-by-one focus view: (index, the decision whose card id or ticket is `at`, else the first, previous
    card id, next card id)."""
    if not decisions:
        return 0, None, None, None
    want = (at or "").upper()
    i = next((k for k, d in enumerate(decisions) if d.cid.upper() == want), None)
    if i is None:
        i = next((k for k, d in enumerate(decisions) if d.ticket.upper() == want), 0)
    prev = decisions[i - 1].cid if i > 0 else None
    nxt = decisions[i + 1].cid if i + 1 < len(decisions) else None
    return i, decisions[i], prev, nxt


def _first_line(text: str) -> str | None:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def in_flight(ws, *, needs, entries, events, now: datetime | None = None, rows=None, settings=None) -> list[dict]:
    """Active tickets (open, in progress, waiting, testing) that do not need the human (a ready
    human task alone does not count while the agent still has a doing or next task), as
    [{id, title, status, steps, next, agent, start, tasks}]. `next` is the first line of "Current
    state" (None when empty); `tasks` is data.tasks.flight() (None without tasks or with a broken
    Tasks section); `agent` is {"text", "role"} for a claimed ticket, else None; `start` is the brief
    Start agent suggestion (agent_start.suggest(brief=True); the one shared panel is built for one ticket) when it is
    the agent's move (None while a fresh claim works on it, while
    the ticket waits on a blocker, or when no harness is known). `rows` (agents.agent_rows) and
    `settings` (launch.load_settings) are computed once here when not given."""
    from orch.dashboard import launch
    from orch.dashboard.data import agent_start
    from orch.dashboard.data.agents import agent_rows

    needing = tasks_data.waiting_on_human(needs, entries)  # a ready human task alone does not stop the agent
    seen: dict[str, int] = {}
    for e in entries:
        seen[e.id.upper()] = seen.get(e.id.upper(), 0) + 1
    picked = [e for e in entries if e.status in IN_FLIGHT and e.meta is not None
              and e.id.upper() not in needing and seen[e.id.upper()] == 1]  # an ambiguous id gets a Repair card
    if not picked:
        return []
    if rows is None:
        rows = agent_rows(ws, now=now, events=events, entries=entries, needs=needs)
    if settings is None:
        settings = launch.load_settings()
    by_ticket: dict[str | None, list] = {}
    for ev in events:
        by_ticket.setdefault(ev.ticket, []).append(ev)
    row_of = {r.ticket: r for r in rows}
    skip = tuple(ws.config["gates"]["plan_skip_sizes"])
    cards = []
    for e in picked:
        try:
            t = store.read_ticket(e.path)
        except (OSError, TicketParseError, UnicodeDecodeError):
            continue
        row = row_of.get(t.id)
        agent = ({"text": f"{row.harness or 'agent'} · {AGENT_LABELS[row.status].lower()}",
                  "role": AGENT_ROLES[row.status]} if row else None)
        start = None
        if t.status != "waiting":
            s = agent_start.suggest(ws, t, needs_items=needs, rows=rows, now=now,
                                    events=by_ticket.get(t.id, []), settings=settings, brief=True)
            start = s if s and not s.get("disabled") else None
        cards.append({"id": t.id, "title": t.title, "status": t.status,
                      "steps": step_list(t, plan_skip_sizes=skip),
                      "next": _first_line(t.section("Current state")), "agent": agent, "start": start,
                      "tasks": tasks_data.flight(tasks_view.view(ws, t, entries, resolve_refs=False))})
    order = {s: i for i, s in enumerate(IN_FLIGHT)}
    return sorted(cards, key=lambda c: (order[c["status"]], c["id"]))


AWAY_HOURS = 24  # "While you were away": the last day's verified events
AWAY_ROWS = 5


def away(ws, *, events, entries, now: datetime | None = None, hours: int = AWAY_HOURS) -> dict:
    """M: what happened without you in the last `hours`, from orch's event log only (fixed phrases, never text an
    agent, a tracker or an addon wrote): {tasks: [{ticket, task, who, at, artifacts}], ideas: n}. `tasks` are the tasks
    agents finished (newest first, AWAY_ROWS at most) with the number of artifacts the ticket ties to that task;
    `ideas` the tickets created in that time that still wait in the backlog. Phone decisions come from
    timeline.phone_receipts (only what a verified phone decision wrote)."""
    import re as _re
    from datetime import timedelta

    from orch.clock import now as clock_now
    from orch.clock import parse_stamp
    from orch.core.artifacts import entries as artifact_entries
    from orch.dashboard.data.timeline import who
    since = (now or clock_now()) - timedelta(hours=hours)
    by_id = {e.id.upper(): e for e in entries}
    tasks, ideas, parsed = [], set(), {}
    for e in reversed(events):
        try:
            at = parse_stamp(e.at)
        except (TypeError, ValueError):
            continue
        if at < since:
            continue  # by timestamp: a merged log need not be in order
        entry = by_id.get(str(e.ticket or "").upper())
        if entry is None:
            continue
        if e.kind == "ticket.created" and entry.status == "backlog":
            ideas.add(entry.id)
        elif (e.kind == "task.moved" and str(e.actor).startswith("agent:") and (e.data or {}).get("now") == "done"
              and len(tasks) < AWAY_ROWS):
            task = str((e.data or {}).get("task") or "")
            if not _re.fullmatch(r"[A-Za-z]{1,3}\d{1,4}", task):
                continue
            if entry.id not in parsed:
                try:
                    parsed[entry.id] = artifact_entries(store.read_ticket(entry.path, shared=True))
                except (OSError, TicketParseError, UnicodeDecodeError):
                    parsed[entry.id] = []
            n = sum(1 for a in parsed[entry.id] if str(a.get("task") or "") == task)
            tasks.append({"ticket": entry.id, "task": task, "who": who(e), "at": at, "artifacts": n})
    return {"tasks": tasks, "ideas": len(ideas)}
