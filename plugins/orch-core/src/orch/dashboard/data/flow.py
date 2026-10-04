"""What the Reports page adds to `metrics.report`: what needs the human now, a record that starts where the log starts,
created and done per day, claim -> testing and testing -> done, and the human's own decisions.

Every figure comes from the event log and the ticket files. Where a figure needs a rule (what counts as imported,
which move counts as "testing"), the rule is in the docstring of the function that applies it."""
from __future__ import annotations

from datetime import datetime, timedelta

from orch.addons.widgets import chart_spec
from orch.clock import now as clock_now
from orch.clock import parse_stamp
from orch.core import events as events_mod
from orch.core import query, store
from orch.dashboard.data import decisions as decisions_mod

# claim -> testing, in minutes: the upper edge of each bucket and its label. Uneven widths on purpose.
BUCKETS = ((30, "< 30 min"), (60, "30–60 min"), (120, "1–2 h"), (240, "2–4 h"), (480, "4–8 h"), (960, "8–16 h"),
           (None, "> 16 h"))


def _parse(stamp) -> datetime | None:
    try:
        return parse_stamp(str(stamp)) if stamp else None
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _stamp_text(at: datetime) -> str:
    local = at.astimezone()
    return f"{local.day} {local:%b}, {local:%H:%M}"


def _day_label(day, short: bool) -> str:
    return f"{day:%a} {day.day} {day:%b}" if short else f"{day.day} {day:%b}"


def needs_you(ws, *, entries, events, at_now: datetime) -> dict:
    """The four counts of "Needs you now", from the same lists Today uses (query.waiting and the decisions built on
    it), so the numbers agree with Today. "Waiting on you" leaves out the verdicts: a ticket in testing is its own
    count. `oldest` names the item that has waited longest (the age of a decision is the time since its ticket's
    newest event)."""
    needs = query.needs_you(ws, entries=entries, events=events)
    waiting = query.waiting(ws, entries=entries, events=events, needs=needs, now=at_now)
    decisions = decisions_mod.decisions(ws, events=events, entries=entries, needs=waiting, now=at_now)
    on_you = [d for d in decisions if d.kind != "verdict"]
    aged = [d for d in on_you if d.age_minutes is not None]
    oldest = max(aged, key=lambda d: d.age_minutes) if aged else None
    open_qs = sum(len(d.questions) for d in decisions if d.kind == "answer")
    asked = answered = 0
    for ev in events:
        if ev.kind == "question.asked":
            asked += len(ev.data.get("qids") or [])
        elif ev.kind == "question.answered":
            answered += 1
    by_status = {"testing": 0, "in-progress": 0}
    for e in entries:
        if e.status in by_status:
            by_status[e.status] += 1
    return {
        "waiting": len(on_you),
        "oldest": None if oldest is None else {
            "ticket": oldest.ticket, "what": decisions_mod.KIND_LABELS.get(oldest.kind, oldest.kind).lower(),
            "age_minutes": oldest.age_minutes},
        "testing": by_status["testing"],
        "questions": open_qs, "asked": asked, "answered": answered,
        "working": by_status["in-progress"],
    }


def overview(ws, *, days: int = 28, now: datetime | None = None) -> dict:
    """Everything the page shows beyond `metrics.report`. See the module docstring; the pieces:

    * `state`: "none" (no event recorded yet), "one-day" (the record is younger than a calendar day boundary: all
      of it is today) or "ok". `since` is the first event, as text; `starts_in_window` is true when it falls inside
      the selected period. Charts start at the first event's day, never before it.
    * `has_before`: a full period before this one exists in the record, so "vs. the period before" is true.
    * created per day: `ticket.created` events are new tickets, by the day of the event. A ticket file with no
      `ticket.created` event in the log was not made through orch (copied in or migrated): it is "imported", counted
      on the day in its `created` field, or on the first recorded day when that is earlier.
    * claim -> testing: first `claim.taken` of a done ticket to the first move into testing after it, elapsed
      (waits included). testing -> done: the last move into testing before the ticket's final done, to that done.
      Only tickets that have both events count; `n_claim`/`n_testing` say how many.
    * decisions: the human's `gate.approved`, `verdict.given` and `question.answered` events in the period.
    """
    at_now = now or clock_now()
    events_all = events_mod.read_events(ws)
    entries = store.scan(ws)
    out: dict = {"days": days, "needs": needs_you(ws, entries=entries, events=events_all, at_now=at_now)}

    stamped = [(ev, _parse(ev.at)) for ev in events_all]
    stamped = sorted([(ev, at) for ev, at in stamped if at is not None], key=lambda p: (p[1], p[0].seq))
    if not stamped:
        out.update(state="none", since=None, first_at=None)
        return out

    first_at = stamped[0][1]
    window_start = at_now - timedelta(days=days)
    local_now = at_now.astimezone()
    first_day = first_at.astimezone().date()
    today = local_now.date()
    days_on_record = (today - first_day).days + 1
    out.update(
        state="one-day" if days_on_record <= 1 else "ok", first_at=first_at, since=_stamp_text(first_at),
        days_on_record=days_on_record, starts_in_window=first_at > window_start,
        has_before=first_at <= at_now - timedelta(days=2 * days))

    by_ticket: dict[str, list[tuple]] = {}
    for ev, at in stamped:
        if ev.ticket:
            by_ticket.setdefault(ev.ticket, []).append((ev, at))

    # --- done in the window: the latest done event inside it, as report() counts a ticket --------------------------
    done_end: dict[str, datetime] = {}
    for tid, tevents in by_ticket.items():
        ends = [at for ev, at in tevents if ev.data.get("to") == "done" and window_start <= at <= at_now]
        if ends:
            done_end[tid] = max(ends)

    claim_min, testing_min = [], []
    for tid, end in done_end.items():
        tevents = [(ev, at) for ev, at in by_ticket[tid] if at <= end]
        claim = next((at for ev, at in tevents if ev.kind == "claim.taken"), None)
        to_testing = [at for ev, at in tevents if ev.kind == "ticket.moved" and ev.data.get("to") == "testing"]
        first_test = next((at for at in to_testing if claim is not None and at >= claim), None)
        if claim is not None and first_test is not None:
            claim_min.append((first_test - claim).total_seconds() / 60)
        if to_testing:
            testing_min.append((end - to_testing[-1]).total_seconds() / 60)
    out.update(
        claim_to_testing_hours=None if not claim_min else _median(claim_min) / 60, n_claim=len(claim_min),
        testing_to_done_hours=None if not testing_min else _median(testing_min) / 60, n_testing=len(testing_min))
    buckets = [0] * len(BUCKETS)
    for m in claim_min:
        buckets[next(i for i, (edge, _l) in enumerate(BUCKETS) if edge is None or m < edge)] += 1
    out["claim_chart"] = chart_spec([label for _e, label in BUCKETS], [("Tickets", buckets)], unit="tickets")

    # --- the human's decisions ---------------------------------------------------------------------------------
    counts = {"gate.approved": 0, "verdict.given": 0, "question.answered": 0}
    for ev, at in stamped:
        if ev.kind in counts and str(ev.actor).startswith("human") and window_start <= at <= at_now:
            counts[ev.kind] += 1
    out["decisions"] = {"gates": counts["gate.approved"], "verdicts": counts["verdict.given"],
                        "answers": counts["question.answered"], "total": sum(counts.values())}

    # --- created and done per day, from the first recorded day ------------------------------------------------------
    start_day = max(first_day, window_start.astimezone().date())
    span = [start_day + timedelta(days=i) for i in range((today - start_day).days + 1)]
    index = {d: i for i, d in enumerate(span)}
    new = [0] * len(span)
    imported = [0] * len(span)
    done = [set() for _ in span]
    created_ids = set()
    for ev, at in stamped:
        day = index.get(at.astimezone().date())
        if ev.kind == "ticket.created" and ev.ticket:
            created_ids.add(ev.ticket)
            if day is not None:
                new[day] += 1
        elif ev.data.get("to") == "done" and ev.ticket and day is not None:
            done[day].add(ev.ticket)
    for e in entries:
        if e.meta is None or e.id in created_ids:
            continue
        made = _parse(e.meta.get("created"))
        day = index.get(max(made.astimezone().date(), first_day)) if made else None
        if day is not None:
            imported[day] += 1
    done_n = [len(s) for s in done]
    short = len(span) <= 14
    series = []
    if any(imported):
        series.append(("Imported", imported, "line2", "created"))
    series += [("New", new, "muted", "created"), ("Done", done_n, "series-3", "done")]
    out["per_day"] = {
        "chart": chart_spec([_day_label(d, short) for d in span], series, stacked=True, unit="tickets"),
        "imported": sum(imported), "new": sum(new), "done": sum(done_n), "days": len(span)}

    # --- the done KPI's subtitle -------------------------------------------------------------------------------------
    out["done_sub_days"] = days_on_record if out["starts_in_window"] else None
    return out

