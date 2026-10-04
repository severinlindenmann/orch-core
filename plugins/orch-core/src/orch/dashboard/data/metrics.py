"""Dashboard metrics derived from the event log."""
from __future__ import annotations

from datetime import datetime, timedelta

from orch.clock import now as clock_now
from orch.clock import parse_stamp
from orch.core import events as events_mod
from orch.core import store
from orch.core.constants import STATUSES
from orch.dashboard.data.text import clean as _clean  # user text (a hand-edited `type`) in the export
from orch.dashboard.data.text import md_escape

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")

WINDOWS = (7, 28, 90)
DAYS_LABELS = {7: "Last 7 days", 28: "Last 4 weeks", 90: "Quarter"}


# One role and one label per ticket status (spec §5), shared by the Reports distribution bar and
# the Board list. `waiting` means a blocking question waits on the human, so it is the `you` role (design-system
# spec D6); `warn` stays for blocked by another ticket, stale and login needed.
STATUS_ROLES = {"backlog": "neu", "open": "neu", "in-progress": "info", "waiting": "you", "testing": "info",
                "done": "ok"}
STATUS_LABELS = {"backlog": "Backlog", "open": "Open", "in-progress": "In progress", "waiting": "Waiting",
                 "testing": "Testing", "done": "Done"}


def status_distribution(ws, *, entries: list | None = None) -> list[dict]:
    """Tickets per status in board order, leaving out statuses with none: [{name, n, role}].

    `entries`: a pre-scanned ticket list shared with the rest of the request; scanned fresh if not given.
    The status is the ticket's folder, so a file that does not parse still counts where it lies.
    """
    counts = {s: 0 for s in STATUSES}
    for e in entries if entries is not None else store.scan(ws):
        if e.status in counts:
            counts[e.status] += 1
    return [{"name": s, "n": n, "role": STATUS_ROLES[s]} for s, n in counts.items() if n]


def done_per_weekday(ws, *, now: datetime | None = None, events: list | None = None) -> list[dict]:
    """Mon-Fri of the current local week: how many distinct tickets moved to done each day.

    `events`: a pre-read event list shared with the rest of the request; read fresh if not given.
    """
    local_now = (now or clock_now()).astimezone()
    monday = (local_now - timedelta(days=local_now.weekday())).date()
    tickets: list[set] = [set() for _ in range(5)]
    for ev in events if events is not None else events_mod.read_events(ws):
        if ev.data.get("to") != "done":
            continue
        try:
            at = parse_stamp(ev.at).astimezone()
        except ValueError:
            continue
        offset = (at.date() - monday).days
        if 0 <= offset < 5:
            tickets[offset].add(ev.ticket)
    counts = [len(t) for t in tickets]
    peak = max(counts)
    out = []
    for day, n in zip(_WEEKDAYS, counts):
        pct = round(n / peak * 100) if peak else 0
        out.append({"d": day, "n": n, "h": f"{pct}%" if pct else "4%"})
    return out


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        return parse_stamp(str(stamp))
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


_STATUS_KINDS = ("waiting", "testing", "in-progress")

def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Collapse overlapping/touching intervals into a non-overlapping union, so e.g. three
    questions asked at once and answered at once don't get added up three times over."""
    out: list[list[datetime]] = []
    for s, e in sorted(intervals, key=lambda iv: iv[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _largest_remainder_split(items: list[tuple[str, float]], total: float) -> list[dict]:
    """Percentages that always sum to exactly 100 (or all "0%" when `total` is 0): floor every
    share, then hand out the leftover whole points to the largest fractional remainders first."""
    if not total:
        return [{"name": name, "pct": "0%"} for name, _ in items]
    shares = [(name, value / total * 100) for name, value in items]
    floors = [int(pct) for _, pct in shares]
    remainder = 100 - sum(floors)
    order = sorted(range(len(shares)), key=lambda i: shares[i][1] - floors[i], reverse=True)
    bumped = set(order[:remainder])
    return [{"name": name, "pct": f"{floors[i] + (1 if i in bumped else 0)}%"}
            for i, (name, _pct) in enumerate(shares)]


def report(ws, *, days: int = 28, now: datetime | None = None) -> dict:
    """Flow metrics for the last `days`, derived from the event log and ticket metadata.

    A ticket counts as "done in the window" if ANY of its events carries `data["to"] == "done"`
    with a timestamp inside the window; its *end time* is the latest such event inside the window.
    So a ticket that was sent back and finished again counts once, at its latest completion.
    `done`/`done_before` and the per-type/median/split figures all key off this same per-ticket
    rule (one ticket, one count, per window).

    The weekly chart is different on purpose: it counts distinct tickets *per week*, over its own
    fixed trailing 8-week range, independent of the selected `days`. A ticket with two "done"
    events in two different weeks of that range appears in both weeks -- reopening and finishing a
    ticket again doesn't erase its earlier week's bar -- but counts once in a week where it has more
    than one qualifying event that week.

    `sent_back`/`verdicts` (the KPI tile) are windowed over ALL tickets: any `verdict.given` event
    whose timestamp falls in the window, whether or not its ticket counts as "done in the window".
    The per-type `sent_back`/`questions` columns are different on purpose: they look at one
    ticket's *whole life up to its end time* (every follow-up verdict, every qid ever asked),
    not just the part of that life inside the window.
    """
    at_now = now or clock_now()
    window_start = at_now - timedelta(days=days)
    prev_start = at_now - timedelta(days=2 * days)

    events: list[tuple] = []
    for ev in events_mod.read_events(ws):
        at = _parse(ev.at)
        if at is not None:
            events.append((ev, at))
    # Chronological order drives every replay below; `seq` only breaks exact-timestamp ties
    # (synthetic/backfilled events are not guaranteed to be appended in timestamp order).
    events.sort(key=lambda pair: (pair[1], pair[0].seq))

    by_ticket: dict[str, list[tuple]] = {}
    for ev, at in events:
        if ev.ticket:
            by_ticket.setdefault(ev.ticket, []).append((ev, at))

    def _done_ats(tevents: list[tuple]) -> list[datetime]:
        return [at for ev, at in tevents if ev.data.get("to") == "done"]

    # --- "done in window" per ticket, and its end time ----------------------
    done_in_window: dict[str, datetime] = {}
    done_before_ids: set[str] = set()
    for tid, tevents in by_ticket.items():
        all_done_ats = _done_ats(tevents)
        in_window = [at for at in all_done_ats if window_start <= at <= at_now]
        if in_window:
            done_in_window[tid] = max(in_window)
        if any(prev_start <= at < window_start for at in all_done_ats):
            done_before_ids.add(tid)

    done = len(done_in_window)
    done_before = len(done_before_ids)

    entries_by_id = {e.id: e for e in store.scan(ws)}

    def _ticket_type(tid: str) -> str:
        e = entries_by_id.get(tid)
        if e is None or e.meta is None:
            return "unknown"
        return _clean(e.meta.get("type") or "unknown")

    def _created(tid: str) -> datetime | None:
        e = entries_by_id.get(tid)
        if e is None or e.meta is None:
            return None
        return _parse(e.meta.get("created"))

    # --- Median open -> done ------------------------------------------------
    days_by_ticket: dict[str, float] = {}
    for tid, end_time in done_in_window.items():
        created = _created(tid)
        if created is None:
            continue
        days_by_ticket[tid] = max(0.0, (end_time - created).total_seconds() / 86400)
    median_days = _median(list(days_by_ticket.values()))

    # --- Question wait times: unaffected by the window a ticket is "done" in;
    # a wait counts if it was *answered* in the window, full stop. ----------
    asked_at: dict[tuple[str, str], datetime] = {}
    waits: list[tuple[datetime, float]] = []  # (answered_at, hours)
    for ev, at in events:
        if ev.kind == "question.asked" and ev.ticket:
            for qid in ev.data.get("qids") or []:
                asked_at[(ev.ticket, str(qid))] = at
        elif ev.kind == "question.answered" and ev.ticket and ev.data.get("qid") is not None:
            key = (ev.ticket, str(ev.data["qid"]))
            started = asked_at.pop(key, None)
            if started is not None:
                waits.append((at, (at - started).total_seconds() / 3600))

    window_waits = [h for answered_at, h in waits if window_start <= answered_at <= at_now]
    median_wait_hours = _median(window_waits)

    # --- Verdicts (KPI tile): windowed over all tickets, regardless of done-ness
    verdict_events = [(ev, at) for ev, at in events
                       if ev.kind == "verdict.given" and window_start <= at <= at_now]
    verdicts = len(verdict_events)
    sent_back = sum(1 for ev, _at in verdict_events if ev.data.get("verdict") == "follow-up")

    # --- Weekly bars: a fixed trailing 8 ISO weeks, independent of `days`; distinct tickets per
    # week (not per window, not deduped across weeks -- see the docstring). -------------------
    local_now = at_now.astimezone()
    monday = (local_now - timedelta(days=local_now.weekday())).date()
    week_starts = [monday - timedelta(weeks=(7 - i)) for i in range(8)]

    week_ticket_ids: list[set] = [set() for _ in range(8)]
    for tid, tevents in by_ticket.items():
        for at in _done_ats(tevents):
            d = at.astimezone().date()
            for i, w_start in enumerate(week_starts):
                if w_start <= d < w_start + timedelta(days=7):
                    week_ticket_ids[i].add(tid)
                    break

    week_counts = [len(ids) for ids in week_ticket_ids]
    peak = max(week_counts)
    weeks = []
    for i, (w_start, n) in enumerate(zip(week_starts, week_counts)):
        # Zero baseline: a week with nothing done draws no bar at all. The last week is the
        # current one and not over yet, so it is labelled "partial".
        pct = round(n / peak * 100) if peak else 0
        weeks.append({"w": f"W{w_start.isocalendar()[1]:02d}", "n": n, "h": f"{pct}%", "partial": i == 7})

    # --- Where the time goes, and the per-type columns, over tickets done in
    # the window -- replaying each one's whole life up to its own end time,
    # and nothing after it. ---------------------------------------------------
    testing_total = waiting_on_you_total = 0.0
    waiting_status_total = waiting_on_you_in_waiting = 0.0
    inprogress_total = waiting_on_you_in_progress = 0.0

    type_done: dict[str, int] = {}
    type_days: dict[str, list[float]] = {}
    type_sent_back: dict[str, int] = {}
    type_questions: dict[str, int] = {}

    for tid, end_time in done_in_window.items():
        life = [(ev, at) for ev, at in by_ticket.get(tid, []) if at <= end_time]

        intervals: list[tuple[str, datetime, datetime]] = []
        cur_status, cur_start = None, None
        for ev, at in life:
            frm, to = ev.data.get("from"), ev.data.get("to")
            if frm is None or to is None:
                continue
            if cur_status is not None:
                intervals.append((cur_status, cur_start, at))
            cur_status, cur_start = to, at
        # The event that set `end_time` (a `to: done`) is itself in `life`, so it always closes
        # the last real interval; nothing past `end_time` was included to begin with.

        # The floor is the earlier of `created` and the ticket's earliest event of ANY kind in its
        # life (not only status changes -- e.g. a question can be asked before the first recorded
        # status change). When `created` is unknown (e.g. a broken file), the earliest event alone
        # is the floor.
        created = _created(tid)
        floor_candidates = [t for t in (created, life[0][1] if life else None) if t is not None]
        floor = min(floor_candidates) if floor_candidates else None

        # Question intervals: asked -> answered, or -> end_time if still open, clipped to
        # [floor, end_time] so an open question on a done ticket never outlives the ticket, then
        # merged into a non-overlapping union so concurrently open questions aren't added up.
        ticket_asked_at: dict[str, datetime] = {}
        qintervals: list[tuple[datetime, datetime]] = []
        for ev, at in life:
            if ev.kind == "question.asked":
                for qid in ev.data.get("qids") or []:
                    ticket_asked_at[str(qid)] = at
            elif ev.kind == "question.answered" and ev.data.get("qid") is not None:
                qid = str(ev.data["qid"])
                started = ticket_asked_at.pop(qid, None)
                if started is not None:
                    qintervals.append((started, at))
        for started in ticket_asked_at.values():
            qintervals.append((started, end_time))

        clipped: list[tuple[datetime, datetime]] = []
        for qs, qe in qintervals:
            s = max(qs, floor) if floor is not None else qs
            e = min(qe, end_time)
            if s < e:
                clipped.append((s, e))
        merged = _merge_intervals(clipped)
        for s, e in merged:
            waiting_on_you_total += (e - s).total_seconds()

        for status, s, e in intervals:
            if status not in _STATUS_KINDS:
                continue
            dur = (e - s).total_seconds()
            if status == "testing":
                testing_total += dur
                continue
            overlap = sum(max(0.0, (min(e, qe) - max(s, qs)).total_seconds()) for qs, qe in merged)
            if status == "waiting":
                waiting_status_total += dur
                waiting_on_you_in_waiting += overlap
            elif status == "in-progress":
                inprogress_total += dur
                waiting_on_you_in_progress += overlap

        # Per-type columns use the ticket's whole life up to `end_time`, not window-scoped.
        t = _ticket_type(tid)
        type_done[t] = type_done.get(t, 0) + 1
        if tid in days_by_ticket:
            type_days.setdefault(t, []).append(days_by_ticket[tid])
        sb = sum(1 for ev, _at in life if ev.kind == "verdict.given" and ev.data.get("verdict") == "follow-up")
        type_sent_back[t] = type_sent_back.get(t, 0) + sb
        qc = sum(len(ev.data.get("qids") or []) for ev, _at in life if ev.kind == "question.asked")
        type_questions[t] = type_questions.get(t, 0) + qc

    blocked_total = max(0.0, waiting_status_total - waiting_on_you_in_waiting)
    agent_working_total = max(0.0, inprogress_total - waiting_on_you_in_progress)
    total = testing_total + waiting_on_you_total + blocked_total + agent_working_total

    split = _largest_remainder_split([
        ("Agent working", agent_working_total),
        ("Waiting on you", waiting_on_you_total),
        ("Testing", testing_total),
        ("Blocked by others", blocked_total),
    ], total)
    # One semantic role per row (spec §5): agent working -> info, waiting on you -> you,
    # testing -> neu (a neutral chart segment, per the spec's outline rule), blocked -> warn.
    split_role = {"Agent working": "info", "Waiting on you": "you", "Testing": "neu",
                  "Blocked by others": "warn"}
    for row in split:
        row["role"] = split_role[row["name"]]

    types = [
        {"type": t, "done": type_done[t], "median_days": _median(type_days.get(t, [])),
         "sent_back": type_sent_back.get(t, 0), "questions": type_questions.get(t, 0)}
        for t in sorted(type_done)
    ]

    return {
        "days": days,
        "done": done,
        "done_before": done_before,
        "median_days": median_days,
        "median_wait_hours": median_wait_hours,
        "median_days_n": len(days_by_ticket),  # the sample sizes the Reports page names next to each median
        "median_wait_n": len(window_waits),
        "sent_back": sent_back,
        "verdicts": verdicts,
        "weeks": weeks,
        "split": split,
        "types": types,
    }


def duration(hours: float | None) -> str:
    """A median duration without false precision: "–" for none, "< 1 h", whole hours under two days, else days
    with one decimal ("2.5 d")."""
    if hours is None:
        return "–"
    if hours < 1:
        return "< 1 h"
    if hours < 48:
        return f"{round(hours)} h"
    return f"{hours / 24:.1f} d"


def report_markdown(r: dict) -> str:
    label = DAYS_LABELS.get(r["days"], f"the last {r['days']} days")
    delta = r["done"] - r["done_before"]
    lines = [f"# Reports — {label}", ""]
    lines.append(f"- Done: {r['done']} ({delta:+d} vs. the period before)")
    lines.append("- Median open -> done: " +
                 (f"{r['median_days']:.1f} days" if r["median_days"] is not None else "–"))
    lines.append("- Waiting on you: " +
                 (f"{r['median_wait_hours']:.1f} h" if r["median_wait_hours"] is not None else "–"))
    lines.append(f"- Sent back: {r['sent_back']} of {r['verdicts']} verdicts")
    lines.append("")
    lines.append("## Tickets done per week")
    for w in r["weeks"]:
        lines.append(f"- {md_escape(w['w'])}: {w['n']}" + (" (partial)" if w.get("partial") else ""))
    lines.append("")
    lines.append("## Where the time goes")
    for s in r["split"]:
        lines.append(f"- {md_escape(s['name'])}: {s['pct']}")
    lines.append("")
    lines.append("## By type")
    for t in r["types"]:
        md = t["median_days"]
        lines.append(f"- {md_escape(t['type'])}: {t['done']} done, "
                     f"{f'{md:.1f} d' if md is not None else '–'} median, "
                     f"{t['sent_back']} sent back, {t['questions']} questions")
    return "\n".join(lines) + "\n"
