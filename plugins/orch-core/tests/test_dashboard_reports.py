import json
import os
import platform
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from orch.clock import now, stamp, stamp_s

# A fixed point in time for the "fix round 1" tests below, so `median_days`/windows/ISO weeks are
# exact instead of depending on when the suite happens to run.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@contextmanager
def _pinned_local_tz(tz_name: str):
    """Pin the process's local timezone for the duration of the `with` block. `report()` buckets
    its weekly chart by *local* date (`datetime.astimezone()`), so a test asserting an exact week
    label must not depend on whatever zone the suite happens to run in."""
    if platform.system() == "Windows":
        pytest.skip("time.tzset() is not available on Windows")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = tz_name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _write_events(ws, rows):
    path = ws.state_dir / "events.jsonl"   # check orch.core.events._path for the real location and use it
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for seq, (minutes_ago, ticket, kind, data) in enumerate(rows, 1):
            f.write(json.dumps({"seq": seq, "at": stamp_s(now() - timedelta(minutes=minutes_ago)), "ticket": ticket,
                                "kind": kind, "actor": "human:you", "via": "cli", "data": data}) + "\n")


def _write_events_at(ws, rows):
    """Like `_write_events`, but each row gives an absolute `at` datetime instead of
    minutes-ago, so a test can place events relative to a fixed `now=` exactly."""
    path = ws.state_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for seq, (at, ticket, kind, data) in enumerate(rows, 1):
            f.write(json.dumps({"seq": seq, "at": stamp_s(at), "ticket": ticket,
                                "kind": kind, "actor": "human:you", "via": "cli", "data": data}) + "\n")


def test_report_counts_and_medians(ws, put):
    from orch.dashboard.data.metrics import report
    a = put("done", title="A")
    _write_events(ws, [
        (3000, a, "ticket.moved", {"from": "open", "to": "in-progress"}),
        (2900, a, "question.asked", {"qids": ["Q1"]}),
        (2840, a, "question.answered", {"qid": "Q1", "answer": "x"}),
        (2000, a, "ticket.moved", {"from": "in-progress", "to": "testing"}),
        (1000, a, "verdict.given", {"verdict": "follow-up", "message": "m", "from": "testing", "to": "in-progress"}),
        (500, a, "ticket.moved", {"from": "in-progress", "to": "testing"}),
        (100, a, "verdict.given", {"verdict": "done", "message": None, "from": "testing", "to": "done"}),
    ])
    r = report(ws, days=28)
    assert r["done"] == 1 and r["sent_back"] == 1 and r["verdicts"] == 2
    assert r["median_wait_hours"] == pytest.approx(1.0, abs=0.05)
    assert len(r["weeks"]) == 8 and sum(w["n"] for w in r["weeks"]) == 1
    assert sum(int(s["pct"].rstrip("%")) for s in r["split"]) == 100


def test_report_empty_workspace(ws):
    from orch.dashboard.data.metrics import report
    r = report(ws)
    assert r["done"] == 0 and r["median_days"] is None and all(s["pct"] == "0%" for s in r["split"])


def test_reports_page_and_markdown(dash):
    assert dash.get("/reports").status_code == 200
    md = dash.get("/reports.md")
    assert md.headers["content-type"].startswith("text/markdown")


@pytest.mark.parametrize("q", ["?days=abc", "?days=5", "?days=-3"])
def test_days_fall_back_to_28(dash, q):
    assert "Last 4 weeks" in dash.get("/reports" + q).text


def test_broken_ticket_does_not_break_reports(dash, ws, put):
    from orch.core import store
    tid = put("done")
    store.resolve(ws, tid).path.write_text("---\nbroken: [\n---\n", encoding="utf-8")
    assert dash.get("/reports").status_code == 200


# --- Fix round 1: reviewer-found contradictions, with a fixed `now=`/`created` so every number
# below is exact rather than "somewhere in a range". ---------------------------------------------

def test_reopen_and_done_again_counts_once(ws, put):
    """A ticket sent back after being marked done, then finished again, is one ticket -- not two
    "done" events -- and its end time (for median/types/weeks) is the *latest* completion."""
    from orch.dashboard.data.metrics import report
    a = put("done", created=stamp(NOW - timedelta(days=10)))
    _write_events_at(ws, [
        (NOW - timedelta(days=5), a, "ticket.moved", {"from": "testing", "to": "done"}),
        (NOW - timedelta(days=4), a, "ticket.moved", {"from": "done", "to": "in-progress"}),
        (NOW - timedelta(days=2), a, "ticket.moved", {"from": "in-progress", "to": "done"}),
    ])
    r = report(ws, now=NOW, days=28)
    assert r["done"] == 1
    assert r["median_days"] == pytest.approx(8.0)  # latest done (day -2) minus created (day -10)
    assert len(r["types"]) == 1 and r["types"][0]["done"] == 1


def test_first_done_outside_window_but_later_done_inside_counts(ws, put):
    """A ticket whose *first* completion is outside the selected window, but that was reopened and
    finished again inside it, still counts -- using the later completion as its end time."""
    from orch.dashboard.data.metrics import report
    a = put("done", created=stamp(NOW - timedelta(days=60)))
    _write_events_at(ws, [
        (NOW - timedelta(days=40), a, "ticket.moved", {"from": "testing", "to": "done"}),
        (NOW - timedelta(days=39), a, "ticket.moved", {"from": "done", "to": "in-progress"}),
        (NOW - timedelta(days=2), a, "ticket.moved", {"from": "in-progress", "to": "done"}),
    ])
    r = report(ws, now=NOW, days=28)
    assert r["done"] == 1
    assert r["median_days"] == pytest.approx(58.0)
    assert len(r["types"]) == 1 and r["types"][0]["done"] == 1 and r["types"][0]["median_days"] == pytest.approx(58.0)
    assert sum(int(s["pct"].rstrip("%")) for s in r["split"]) == 100
    assert {s["name"]: s["pct"] for s in r["split"]} == {
        "Agent working": "100%", "Waiting on you": "0%", "Testing": "0%", "Blocked by others": "0%",
    }


def test_open_question_on_done_ticket_does_not_exceed_ticket_life(ws, put):
    """A question asked but never answered must stop counting at the ticket's own end time, not
    extend all the way to the real "now" the report happens to run at."""
    from orch.dashboard.data.metrics import report
    a = put("done")
    _write_events_at(ws, [
        (NOW - timedelta(days=20), a, "ticket.moved", {"from": "open", "to": "in-progress"}),
        (NOW - timedelta(days=19), a, "question.asked", {"qids": ["Q1"]}),
        (NOW - timedelta(days=18), a, "ticket.moved", {"from": "in-progress", "to": "testing"}),
        (NOW - timedelta(days=17), a, "ticket.moved", {"from": "testing", "to": "done"}),
    ])
    r = report(ws, now=NOW, days=28)
    assert sum(int(s["pct"].rstrip("%")) for s in r["split"]) == 100
    assert {s["name"]: s["pct"] for s in r["split"]} == {
        "Agent working": "25%", "Waiting on you": "50%", "Testing": "25%", "Blocked by others": "0%",
    }


def test_split_rounding_sums_to_exactly_100(ws, put):
    from orch.dashboard.data.metrics import report
    a = put("done")
    base = NOW - timedelta(hours=100)
    _write_events_at(ws, [
        (base, a, "ticket.moved", {"from": "open", "to": "in-progress"}),
        (base + timedelta(minutes=90), a, "ticket.moved", {"from": "in-progress", "to": "waiting"}),
        (base + timedelta(minutes=180), a, "ticket.moved", {"from": "waiting", "to": "testing"}),
        (base + timedelta(minutes=270), a, "ticket.moved", {"from": "testing", "to": "done"}),
    ])
    r = report(ws, now=NOW, days=28)
    assert sum(int(s["pct"].rstrip("%")) for s in r["split"]) == 100


def test_median_days_done_before_types_and_markdown(ws, put):
    from orch.dashboard.data.metrics import report, report_markdown
    a = put("done", type="feature", created=stamp(NOW - timedelta(days=3)))
    b = put("done", type="bug", created=stamp(NOW - timedelta(days=50)))
    _write_events_at(ws, [
        (NOW - timedelta(days=1), a, "ticket.moved", {"from": "testing", "to": "done"}),
        (NOW - timedelta(days=35), b, "ticket.moved", {"from": "testing", "to": "done"}),  # previous window only
    ])
    r = report(ws, now=NOW, days=28)
    assert r["done"] == 1        # only `a`'s completion falls in the current 28-day window
    assert r["done_before"] == 1  # only `b`'s falls in the (28, 56] days-ago window before it
    assert r["median_days"] == pytest.approx(2.0)
    assert r["types"] == [{"type": "feature", "done": 1, "median_days": pytest.approx(2.0),
                            "sent_back": 0, "questions": 0}]
    md = report_markdown(r)
    assert "+-" not in md
    assert "- Done: 1 (+0 vs. the period before)" in md


def test_split_ignores_events_after_the_ticket_end_time(ws, put):
    """A ticket reached "done" and was later reopened again (its live status is no longer
    "done"); the reopen event happened *after* the window's end time, so it -- and anything
    after it -- must not feed into the split for the earlier completion."""
    from orch.dashboard.data.metrics import report
    a = put("in-progress")
    _write_events_at(ws, [
        (NOW - timedelta(days=10), a, "ticket.moved", {"from": "open", "to": "in-progress"}),
        (NOW - timedelta(days=9), a, "ticket.moved", {"from": "in-progress", "to": "done"}),
        (NOW - timedelta(days=8), a, "ticket.moved", {"from": "done", "to": "testing"}),
    ])
    r = report(ws, now=NOW, days=28)
    assert r["done"] == 1
    assert r["split"] == [
        {"name": "Agent working", "pct": "100%", "role": "info"},
        {"name": "Waiting on you", "pct": "0%", "role": "you"},
        {"name": "Testing", "pct": "0%", "role": "neu"},
        {"name": "Blocked by others", "pct": "0%", "role": "warn"},
    ]


@pytest.mark.parametrize("tz_name", ["UTC", "Europe/Zurich"])
def test_week_label_is_zero_padded_and_timezone_independent(ws, put, tz_name):
    """Two completions of the same ticket, both on weekdays safely in the middle of the day and
    well inside the same ISO week (no event near a week/day boundary), count once in that week,
    under a zero-padded "W01" label rather than "W1". The local timezone is pinned for the
    duration of the test (`report()`'s weekly chart buckets by local date, i.e. `astimezone()`),
    and this is parametrized over UTC and a UTC+1/+2 zone so the result can't depend on whichever
    zone the suite happens to run under."""
    from orch.dashboard.data.metrics import report
    a = put("done")
    at_now = datetime(2027, 1, 6, 12, tzinfo=timezone.utc)  # Wednesday, deep inside ISO week 2027-W01
    with _pinned_local_tz(tz_name):
        _write_events_at(ws, [
            (datetime(2027, 1, 4, 10, 0, tzinfo=timezone.utc), a, "ticket.moved", {"from": "testing", "to": "done"}),
            (datetime(2027, 1, 5, 10, 0, tzinfo=timezone.utc), a, "ticket.moved", {"from": "testing", "to": "done"}),
        ])
        r = report(ws, now=at_now, days=28)
    assert all(re.fullmatch(r"W\d{2}", w["w"]) for w in r["weeks"])
    assert sum(w["n"] for w in r["weeks"]) == 1
    assert any(w["w"] == "W01" and w["n"] == 1 for w in r["weeks"])


def test_weekly_bars_count_per_week_not_deduped_across_weeks(ws, put):
    """Controller ruling: the weekly chart counts distinct tickets *per week*. A ticket finished,
    reopened, and finished again in a later week shows up in BOTH weeks' bars -- reopening a
    ticket doesn't erase its earlier completion's history -- while the `done` KPI (for the
    selected window) still counts it once, as a single distinct ticket."""
    from orch.dashboard.data.metrics import report
    a = put("done", created=stamp(NOW - timedelta(days=30)))
    _write_events_at(ws, [
        (NOW - timedelta(days=20), a, "ticket.moved", {"from": "testing", "to": "done"}),
        (NOW - timedelta(days=19), a, "ticket.moved", {"from": "done", "to": "in-progress"}),
        (NOW - timedelta(days=2), a, "ticket.moved", {"from": "in-progress", "to": "done"}),
    ])
    r = report(ws, now=NOW, days=28)
    assert r["done"] == 1  # the KPI stays a single distinct ticket for the window
    nonzero_weeks = [w for w in r["weeks"] if w["n"] > 0]
    assert sum(w["n"] for w in r["weeks"]) == 2
    assert len(nonzero_weeks) == 2 and all(w["n"] == 1 for w in nonzero_weeks)


def test_concurrent_questions_are_not_added_up(ws, put):
    """Three questions asked and answered at the same moments must count as ONE open interval,
    not three stacked on top of each other, both in "waiting on you" and in the overlap taken out
    of "in-progress"."""
    from orch.dashboard.data.metrics import report
    a = put("done")
    _write_events_at(ws, [
        (NOW - timedelta(hours=30), a, "ticket.moved", {"from": "open", "to": "in-progress"}),
        (NOW - timedelta(hours=30), a, "question.asked", {"qids": ["Q1", "Q2", "Q3"]}),
        (NOW - timedelta(hours=20), a, "question.answered", {"qid": "Q1", "answer": "x"}),
        (NOW - timedelta(hours=20), a, "question.answered", {"qid": "Q2", "answer": "x"}),
        (NOW - timedelta(hours=20), a, "question.answered", {"qid": "Q3", "answer": "x"}),
        (NOW - timedelta(hours=20), a, "ticket.moved", {"from": "in-progress", "to": "testing"}),
        (NOW - timedelta(hours=10), a, "ticket.moved", {"from": "testing", "to": "done"}),
    ])
    r = report(ws, now=NOW, days=28)
    assert sum(int(s["pct"].rstrip("%")) for s in r["split"]) == 100
    assert {s["name"]: s["pct"] for s in r["split"]} == {
        "Agent working": "0%", "Waiting on you": "50%", "Testing": "50%", "Blocked by others": "0%",
    }


def test_floor_falls_back_to_earliest_event_when_created_is_missing(ws, put):
    """When `created` can't be read (e.g. a broken ticket file), the floor used to clip question
    intervals is the ticket's earliest event of any kind -- not only status changes -- so a
    question asked before the first status change still counts, instead of being clipped away."""
    from orch.core import store
    from orch.dashboard.data.metrics import report
    a = put("done")
    _write_events_at(ws, [
        (NOW - timedelta(days=5), a, "question.asked", {"qids": ["Q1"]}),  # before any status change
        (NOW - timedelta(days=4), a, "question.answered", {"qid": "Q1", "answer": "x"}),
        (NOW - timedelta(days=3), a, "ticket.moved", {"from": "open", "to": "in-progress"}),
        (NOW - timedelta(days=1), a, "ticket.moved", {"from": "in-progress", "to": "done"}),
    ])
    store.resolve(ws, a).path.write_text("---\nbroken: [\n---\n", encoding="utf-8")  # `created` unreadable
    r = report(ws, now=NOW, days=28)
    # 2 days of in-progress (day -3 to day -1), with the question's full 1-day wait (day -5 to
    # day -4) entirely before the in-progress interval started, so it contributes to "waiting on
    # you" in full but has no overlap left to subtract from "agent working". If the question had
    # instead been clipped to the old (wrong) floor of "first status change" (day -3), its interval
    # would invert (start after its own end) and disappear from the split entirely.
    assert sum(int(s["pct"].rstrip("%")) for s in r["split"]) == 100
    split = {s["name"]: s["pct"] for s in r["split"]}
    assert split["Agent working"] == "67%" and split["Waiting on you"] == "33%"
