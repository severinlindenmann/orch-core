from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from orch.clock import now as clock_now, parse_stamp
from orch.core import store
from orch.core.constants import PRIORITY_RANK, STATUSES, SUCCEEDED
from orch.core.gates import GATE_SECTIONS, changes_pending, gate_state
from orch.core.ids import normalize_ref
from orch.errors import UsageError


def _num(ticket_id: str) -> int:
    m = re.search(r"(\d+)$", ticket_id)
    return int(m.group(1)) if m else 0


def _sort_key(e: store.Entry):
    return (STATUSES.index(e.status), _num(e.id))


def list_tickets(ws, *, status: str | None = None, label: str | None = None, session: str | None = None) -> list[store.Entry]:
    if status is not None and status not in STATUSES:
        raise UsageError(f"unknown status {status!r}", hint="one of: " + ", ".join(STATUSES))
    out = []
    for e in store.scan(ws):
        if status and e.status != status:
            continue
        if label or session:
            if e.meta is None:
                continue
            labels = e.meta.get("labels")
            if label and not (isinstance(labels, list) and label in labels):
                continue
            claim = e.meta.get("claim")
            if session and (claim.get("session") if isinstance(claim, dict) else None) != session:
                continue
        out.append(e)
    return sorted(out, key=_sort_key)


def idle_days(ws, meta: dict, status: str, now: datetime | None = None) -> int | None:
    """Days since an open or backlog ticket was last touched, when that is at least `dashboard.revalidate_days`
    (0 turns it off); else None. Freshness derived, never stored: such a ticket is checked against the code again
    before anyone builds it. It blocks nobody, so it is no need (NEEDS_ORDER)."""
    limit = (ws.config.get("dashboard") or {}).get("revalidate_days")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or status not in ("backlog", "open"):
        return None
    try:
        then = parse_stamp(str(meta.get("updated") or "")) if isinstance(meta.get("updated"), str) else None
    except ValueError:
        return None
    if then is None:
        return None
    days = ((now or clock_now()) - then).days
    return days if days >= limit else None


def next_tickets(ws, today=None) -> list[store.Entry]:
    """Open, unblocked tickets by priority; within a priority, the overdue and due-soon ones first (earliest due
    first, orch.core.due), then by creation."""
    from orch.core.due import due_state, parse_due, today as local_today
    today = today or local_today()
    entries = store.scan(ws)
    done = {e.id.upper() for e in entries if e.status == "done"}
    candidates = [
        e for e in entries
        if e.status == "open" and e.meta
        and all(normalize_ref(ws, str(b)).upper() in done for b in e.meta.get("blocked_by") or [])
    ]

    def key(e: store.Entry):
        pressing = due_state(e.meta, e.status, today) is not None
        return (PRIORITY_RANK.get(e.meta.get("priority"), 2), not pressing,
                parse_due(e.meta.get("due")).isoformat() if pressing else "", str(e.meta.get("created") or ""), _num(e.id))
    return sorted(candidates, key=key)


def search(ws, text: str) -> list[store.Entry]:
    needle = text.lower()
    hits = [e for e in store.scan(ws) if needle in e.path.read_text(encoding="utf-8", errors="replace").lower()]
    return sorted(hits, key=_sort_key)


def artifact_list(ws, ticket_id: str) -> list[Path]:
    d = ws.artifacts_dir / ticket_id
    return sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else []


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def artifact_file(ws, ticket_id: str, name: str) -> Path | None:
    """The file `name` inside the ticket's own artifact folder, or None: `..`, symlinks and absolute names that
    resolve outside it (or into another ticket's folder) are refused, as is a ticket id that escapes the root, and
    nothing is found when the artifacts folder itself is a symlink or lies outside the workspace."""
    root = ws.artifacts_dir.resolve()
    if ws.artifacts_dir.is_symlink() or not _under(root, ws.home.resolve()):
        return None
    base = (root / ticket_id).resolve()
    target = (base / name).resolve()
    if _under(base, root) and base != root and _under(target, base) and target.is_file():
        return target
    return None


def artifact_root(ws, ticket_id: str) -> Path:
    """The ticket's artifact folder, resolved: where a hardened open of one of its files starts."""
    return (ws.artifacts_dir.resolve() / ticket_id).resolve()


def ticket_view(ws, path: Path, ticket) -> dict:
    from orch.core import tasks_view
    return {
        "id": ticket.id,
        "title": ticket.title,
        "status": ticket.status,
        "path": path.relative_to(ws.home).as_posix(),
        "meta": ticket.meta,
        "sections": ticket.sections,
        "gates": {g: gate_state(ticket, g) for g in GATE_SECTIONS},
        "artifacts": [p.relative_to(ws.artifacts_dir).as_posix() for p in artifact_list(ws, ticket.id)],
        "tasks": tasks_view.view(ws, ticket),
    }


# Kind order inside one scope. Blocking items stop an agent (or would, the moment it picks the ticket up): a broken
# file, an open question, a plan or re-approval, requirements a refine agent waits on, the last human task, a verdict,
# a silent claim. Backlog items (requirements on an unclaimed backlog ticket) are grooming. "Later" items stop nobody:
# a human task while the agent still has work, and a non-blocking question the agent went ahead on (#12: "confirm").
NEEDS_ORDER = {"broken": 0, "answer": 1, "approve-plan": 2, "re-approve": 3, "approve-epic": 4, "approve-requirements": 5,
               "task": 6, "verdict": 7, "stale-claim": 8, "confirm": 9}  # approve-epic: children wait on it (E2)
NEEDS_LABELS = {
    "broken": "repair the ticket file",
    "answer": "answer",
    "task": "do your task",
    "approve-requirements": "approve requirements",
    "approve-plan": "approve plan",
    "re-approve": "re-approve",
    "approve-epic": "re-approve the epic",
    "verdict": "give a verdict",
    "stale-claim": "release or check the silent claim",
    "confirm": "confirm the agent's assumption",
    "factory-ready": "the factory is ready for your verdict",
    "factory-stopped": "the factory stopped",
}
# AI Factory phase 3 (orch.core.factory_report): derived items of waiting(), never of needs_you() (they depend on
# the ledger and the clock, not only on the ticket files) and never a Decision card of their own; Today draws them.
FACTORY_KINDS = ("factory-ready", "factory-stopped")
_FACTORY_RANK = {"factory-stopped": 4, "factory-ready": 7}  # beside approve-epic and verdict
SCOPES = ("blocking", "later", "backlog")  # also the list order
_SCOPE_RANK = {s: i for i, s in enumerate(SCOPES)}


def _order(item: dict) -> tuple:
    return (_SCOPE_RANK[item["scope"]], NEEDS_ORDER.get(item["kind"], _FACTORY_RANK.get(item["kind"], 9)), PRIORITY_RANK.get(item.get("priority"), 2),
            _num(item["ticket"]))


def counts(items: list[dict]) -> dict:
    """{"blocking", "backlog", "later", "total"} of `waiting()` (or needs_you) items: the one source of every count.
    The headline, the menu badge, the tab title, the workspace switcher and the SessionStart hook show `blocking`."""
    out = {s: 0 for s in ("blocking", "backlog", "later")}
    for i in items:
        out[i.get("scope", "blocking")] += 1
    return {**out, "total": len(items)}


def resolution(meta: dict | None, status: str) -> str | None:
    """Why a done ticket is done; null while it is not. A done ticket without one (closed before resolutions) is completed."""
    return (str((meta or {}).get("resolution") or "") or "completed") if status == "done" else None


def open_blockers(ws, ticket, entries: list | None = None) -> list[str]:
    """`blocked_by` entries that are not done yet; unknown references count as blocking.
    `entries` (a `store.scan`) may be shared with the caller; it is scanned here when not given."""
    refs = ticket.meta.get("blocked_by") or []
    if not refs:
        return []
    entries = store.scan(ws) if entries is None else entries
    out = []
    for ref in refs:
        try:
            entry = store.resolve(ws, str(ref), entries)
        except UsageError:  # includes NotFoundError
            out.append(str(ref))
            continue
        seen = set()  # a superseded or duplicate blocker hands the block on to the ticket that replaced it
        while (entry.status == "done" and (entry.meta or {}).get("resolution") in SUCCEEDED
               and entry.id not in seen):
            seen.add(entry.id)
            try:
                entry = store.resolve(ws, str(entry.meta.get("superseded_by")), entries)
            except UsageError:
                break
        if entry.status != "done":
            out.append(entry.id)
    return out


# needs_you() results per workspace, reused while no ticket file (path, folder, mtime, size) and no
# relevant config value changed. The dashboard computes it on every page load.
_NEEDS_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def _fingerprint(ws, entries: list[store.Entry]) -> tuple:
    files = []
    for e in entries:
        try:
            st = store.stat(e.path)
        except OSError:
            return ()  # vanished mid-scan: never cache this round
        files.append((str(e.path), e.status, st.st_mtime_ns, st.st_size, st.st_ctime_ns))
    return (tuple(ws.config["gates"]["plan_skip_sizes"]), tuple(files))


def needs_you(ws, *, entries: list[store.Entry] | None = None, events: list | None = None) -> list[dict]:
    """Everything that is waiting on the human, most urgent kind first.

    `entries`: a `store.scan(ws)` result the caller already holds (one scan per request).
    `events`: a `read_events(ws)` result the caller already holds, used only for a "verdict"
    item's testing round; read once here (not per ticket) when a testing ticket needs it and
    nothing was given.
    """
    entries = store.scan(ws) if entries is None else entries
    key, fp = str(ws.home), _fingerprint(ws, entries)
    cached = _NEEDS_CACHE.get(key)
    if fp and cached and cached[0] == fp:
        return [dict(i) for i in cached[1]]
    items = _needs_you(ws, entries, events)
    if fp:
        _NEEDS_CACHE[key] = (fp, [dict(i) for i in items])
    return items


def blocking_ids(items: list[dict]) -> set[str]:
    """Upper-case ids of the tickets with a blocking item: the ones an agent waits on the human for."""
    return {str(i.get("ticket", "")).upper() for i in items if i.get("scope", "blocking") == "blocking"}


def claim_last_at(claim: dict, ticket_events) -> datetime | None:
    """The newest of the claim's own time and the ticket's events: the last sign of life of the agent holding it."""
    from orch.clock import parse_stamp

    def parsed(s) -> datetime | None:
        try:
            return parse_stamp(str(s)) if s else None
        except ValueError:
            return None

    last = parsed(claim.get("at"))
    for ev in ticket_events:
        at = parsed(ev.at)
        if at is not None and (last is None or at > last):
            last = at
    return last


def claim_expiry(ws, ticket_id: str, status: str, claim: dict, events=None) -> tuple[bool, datetime | None]:
    """(expired, last sign of life) of a ticket's claim: the one rule `claims.ttl_hours` is measured by (#217).
    It runs from the holder's last sign of life (`claim_last_at`: the claim, then the ticket's events), not from
    `claim.at`, and never ends while the ticket is waiting on the human (the agent is parked, not gone). No claim
    is expired. `events`: the ticket's events when the caller already holds them."""
    if not claim.get("session"):
        return True, None
    if events is None:
        from orch.core.events import read_events
        events = read_events(ws, ticket_id)
    last = claim_last_at(claim, events)
    if status == "waiting":
        return False, last
    ttl = float(ws.config["claims"]["ttl_hours"])
    return last is None or (clock_now() - last).total_seconds() > ttl * 3600, last


def claim_is_expired(ws, ticket_id: str, status: str, claim: dict, events=None) -> bool:
    return claim_expiry(ws, ticket_id, status, claim, events)[0]


def is_silent(ws, last_at: datetime | None, now: datetime) -> bool:
    """A claim with no sign of life for `dashboard.stale_minutes` (the Activity page's "Stale")."""
    return last_at is None or last_at < now - timedelta(minutes=int(ws.config["dashboard"]["stale_minutes"]))


def _silent_for(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h" if minutes < 24 * 60 else f"{minutes // (24 * 60)} d"


def stale_claims(ws, *, entries: list[store.Entry], needs: list[dict], events: list, now: datetime) -> list[dict]:
    """A "stale-claim" item per claimed, not-done ticket whose agent has been silent for `stale_minutes` and that does
    not wait on the human (that agent is not stuck, it waits for you). Time-based, so never part of the needs cache."""
    waiting_ids = blocking_ids(needs)
    by_ticket: dict[str | None, list] = {}
    for ev in events:
        by_ticket.setdefault(ev.ticket, []).append(ev)
    out = []
    for e in entries:
        claim = e.meta.get("claim") if e.meta is not None else None
        if e.status == "done" or not isinstance(claim, dict) or not claim.get("session"):
            continue
        if e.id.upper() in waiting_ids:
            continue
        last = claim_last_at(claim, by_ticket.get(e.id, []))
        if not is_silent(ws, last, now):
            continue
        harness = str(claim.get("harness") or "agent")
        silent = f"silent {_silent_for(max(0, int((now - last).total_seconds() // 60)))}" if last else "no activity"
        out.append({"ticket": e.id, "title": e.meta.get("title") or "", "status": e.status, "kind": "stale-claim",
                    "detail": f"{harness} {silent}", "scope": "blocking", "priority": e.meta.get("priority"),
                    "harness": harness, "last_at": last})
    return out


def waiting(ws, *, entries: list[store.Entry] | None = None, events: list | None = None,
            needs: list[dict] | None = None, now: datetime | None = None) -> list[dict]:
    """Everything waiting on the human, in list order: needs_you() plus the silent claims. Every surface that shows a
    count or a list of the human's items uses this (with `counts()`), so they never disagree. `entries`, `events` and
    `needs` may be shared with the caller; the event log is read once per request scope, and only with a claim."""
    from orch.clock import now as clock_now
    from orch.core.events import read_events
    entries = store.scan(ws) if entries is None else entries
    needs = needs_you(ws, entries=entries, events=events) if needs is None else needs
    if any(e.meta is not None and isinstance(e.meta.get("claim"), dict) and e.meta["claim"].get("session")
           and e.status != "done" for e in entries):
        if events is None:
            events = store.memo(ws, "events", lambda: read_events(ws))
        needs = needs + stale_claims(ws, entries=entries, needs=needs, events=events, now=now or clock_now())
    from orch.core import factory_report
    needs = needs + factory_report.items(ws, entries, events)  # empty unless factory.enabled
    return sorted(needs, key=_order)


def _needs_you(ws, entries: list[store.Entry], events: list | None = None) -> list[dict]:
    from orch.core import tasks as tk
    from orch.core.gates import gate_hash, invalidated_gates, plan_required
    from orch.core.lifecycle import unanswered_blocking
    from orch.core.questions import question_hash
    from orch.errors import TicketParseError

    from orch.core import epics

    items: list[dict] = []
    by_ticket_events: dict[str | None, list] | None = None
    seen_ids: dict[str, int] = {}
    epic_meta = {x.id.upper(): x.meta for x in entries if x.meta is not None and epics.is_epic(x.meta)}
    for e in entries:
        seen_ids[e.id.upper()] = seen_ids.get(e.id.upper(), 0) + 1
    reported: set[str] = set()
    for e in entries:
        if seen_ids[e.id.upper()] > 1:  # one id in two files: one blocking repair item, whatever the files hold
            if e.id.upper() not in reported:
                reported.add(e.id.upper())
                items.append({"ticket": e.id, "title": (e.meta or {}).get("title"), "status": e.status,
                              "kind": "broken", "detail": f"{e.id} is held by {seen_ids[e.id.upper()]} files",
                              "scope": "blocking", "priority": None})
            continue
        if e.status == "done":
            continue
        try:
            t = store.read_ticket(e.path) if e.meta is not None else None
        except (TicketParseError, UnicodeDecodeError):
            t = None
        if t is None:
            items.append({"ticket": e.id, "title": None, "status": e.status, "kind": "broken",
                          "detail": e.error or "", "scope": "blocking", "priority": None})
            continue

        claim = t.meta.get("claim")
        claimed = isinstance(claim, dict) and bool(claim.get("session"))
        grooming = e.status == "backlog" and not claimed  # nobody works on it: its approvals are backlog grooming

        def add(kind: str, detail: str = "", scope: str = "blocking", **extra) -> None:
            items.append({"ticket": t.id, "title": t.title, "status": e.status, "kind": kind, "detail": detail,
                          "scope": scope, "priority": t.meta.get("priority"), **extra})

        open_qs = unanswered_blocking(t)
        if open_qs:
            add("answer", ", ".join(str(q.get("id")) for q in open_qs),
                hashes={str(q.get("id")): question_hash(q) for q in open_qs})
        is_epic = epics.is_epic(t)
        if (e.status == "backlog" and not open_qs and gate_state(t, "requirements") == "pending"
                and t.section("Requirements").strip() and t.section("Acceptance criteria").strip()
                and not changes_pending(t, "requirements")):
            # an epic's approval binds its charter: the dashboard sends the charter's content hash back
            # F2: the agent drafted the plan too, so the human may approve both gates in one confirm
            together = (not is_epic and plan_required(ws, t) and gate_state(t, "plan") == "pending"
                        and bool(t.section("Plan").strip()) and not changes_pending(t, "plan"))
            add("approve-requirements", scope="backlog" if grooming else "blocking", gate="requirements",
                gate_hash=epics.charter(ws, t, entries=entries)["content_hash"] if is_epic else gate_hash(t, "requirements"),
                **({"together": True, "plan_hash": gate_hash(t, "plan")} if together else {}))
        if is_epic:
            if e.status == "open" and not changes_pending(t, "requirements"):
                pending = _epic_needs(ws, t, entries, events)
                if pending is not None:
                    add("approve-epic", ", ".join(pending), gate="requirements",
                        gate_hash=epics.charter(ws, t, entries=entries)["content_hash"], children=pending)
        in_epic = epic_meta.get(normalize_ref(ws, str(t.meta.get("parent") or "")).upper())
        if (e.status in ("in-progress", "waiting") and plan_required(ws, t)
                and gate_state(t, "plan") == "pending" and t.section("Plan").strip()
                and not changes_pending(t, "plan")):
            add("approve-plan", gate="plan", gate_hash=gate_hash(t, "plan"))
        for gate in () if is_epic else GATE_SECTIONS:  # an epic's changed text is part of approve-epic
            if gate_state(t, gate) == "invalidated" and not changes_pending(t, gate):
                if in_epic is not None and ((t.meta.get("gates") or {}).get(gate) or {}).get("epic") \
                        and epics.epic_approved(in_epic):
                    continue  # approved with the epic: the epic's re-approval is the decision (approve-epic)
                add("re-approve", gate, scope="backlog" if grooming else "blocking", gate=gate,
                    gate_hash=gate_hash(t, gate))
        if e.status == "testing" and not invalidated_gates(t):  # a changed gate is re-approved before a verdict
            from orch.core.events import read_events, latest_testing_round
            if by_ticket_events is None:
                by_ticket_events = {}
                for ev in (events if events is not None else read_events(ws)):
                    by_ticket_events.setdefault(ev.ticket, []).append(ev)
            add("verdict", round=latest_testing_round(by_ticket_events.get(t.id, [])))
        if e.status in ("in-progress", "waiting"):
            try:
                ticket_tasks = tk.ticket_tasks(t)
                # A ready human task stops the agent only once it has no doing or next task of its own.
                busy = tk.next_task(ticket_tasks) is not None
                for task in tk.human_ready(ticket_tasks):
                    add("task", task.id, scope="later" if busy else "blocking")
            except tk.TaskParseError as err:
                add("broken", err.message)
        unconfirmed = [q for q in t.meta.get("questions") or [] if isinstance(q, dict)
                       and not q.get("blocking", True) and q.get("answer") in (None, "")]
        if unconfirmed:  # #12: the agent went ahead on its recommendation; the human may confirm or change it
            add("confirm", ", ".join(str(q.get("id")) for q in unconfirmed), scope="later",
                hashes={str(q.get("id")): question_hash(q) for q in unconfirmed})
    return sorted(items, key=_order)


def _epic_needs(ws, epic, entries, events=None) -> list[str] | None:
    """The children an approved epic's re-approval would cover anew (changed, added, or auto-approved under a
    delegation that no longer holds), when its requirements changed or such children exist and every one of them is
    ready to approve; else None. An empty list: only the epic's own text changed."""
    from orch.core import epics
    from orch.core.lifecycle import unanswered_blocking
    from orch.errors import TicketParseError
    if not epics.epic_approved(epic):
        return None
    signed, events = epics._signed(ws, None), epics._events(ws, events)  # one read each for every child
    out = []
    for c in epics.children(ws, epic.id, entries):
        if c.status == "done":
            continue
        try:
            ct = store.read_ticket(c.path)
        except (TicketParseError, UnicodeDecodeError, OSError):
            return None
        if epics.child_state(ws, epic, ct, signed, events, entries) in ("changed", "new", "paused"):
            if not (ct.section("Requirements").strip() and ct.section("Acceptance criteria").strip()) \
                    or unanswered_blocking(ct) or epics.is_epic(ct):
                return None  # still being refined: the agent's move, not the human's
            out.append(ct.id)
    if out or gate_state(epic, "requirements") == "invalidated":
        return out
    return None
