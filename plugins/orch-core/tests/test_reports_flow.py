"""Reports: what needs you now, a record that starts at the first event, created per day split imported/new, claim ->
testing, your own decisions, and the states for no record and one day."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from orch.clock import stamp, stamp_s  # noqa: E402
from test_dashboard_reports import _pinned_local_tz  # noqa: E402

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _events(ws, rows):
    path = ws.state_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for seq, (at, ticket, kind, data, *actor) in enumerate(rows, 1):
            f.write(json.dumps({"seq": seq, "at": stamp_s(at), "ticket": ticket, "kind": kind,
                                "actor": actor[0] if actor else "human:you", "via": "cli", "data": data}) + "\n")


def _spec(html, fid):
    m = re.search(rf'<figure class="chart" data-chart=\'([^\']*)\'[^>]*id="{fid}"', html)
    assert m, fid
    return json.loads(m.group(1))


def _two_days(ws, put):
    """Day 1 (3 Oct): one ticket made through orch; two tickets copied in (no ticket.created event). Day 2: done."""
    new = put("done", created=stamp(NOW - timedelta(days=1, hours=2)))
    put("backlog", created=stamp(NOW - timedelta(days=40)))  # imported, created before the record began
    put("backlog", created=stamp(NOW - timedelta(days=1, hours=1)))  # imported on day 1
    d1 = NOW - timedelta(days=1, hours=2)
    _events(ws, [
        (d1, new, "ticket.created", {"title": "x"}, "agent:claude"),
        (d1 + timedelta(minutes=5), new, "gate.approved", {"gate": "requirements"}),
        (d1 + timedelta(minutes=10), new, "claim.taken", {}, "agent:claude"),
        (d1 + timedelta(minutes=70), new, "ticket.moved", {"from": "in-progress", "to": "testing"}, "agent:claude"),
        (d1 + timedelta(minutes=80), new, "verdict.given", {"verdict": "accepted"}),
        (NOW - timedelta(hours=1), new, "ticket.moved", {"from": "testing", "to": "done"}),
        (NOW - timedelta(hours=2), new, "question.asked", {"qids": ["q1"]}, "agent:claude"),
        (NOW - timedelta(hours=1, minutes=30), new, "question.answered", {"qid": "q1"}),
    ])
    return new


def test_the_record_starts_at_the_first_event_and_imports_are_split(ws, put):
    from orch.dashboard.data.flow import overview
    with _pinned_local_tz("UTC"):
        _two_days(ws, put)
        o = overview(ws, days=28, now=NOW)
    assert o["state"] == "ok" and o["days_on_record"] == 2 and o["starts_in_window"] and not o["has_before"]
    chart = o["per_day"]["chart"]
    assert chart["labels"] == ["Sat 3 Oct", "Sun 4 Oct"]  # nothing before the first event, no empty weeks
    by = {s["name"]: s["values"] for s in chart["series"]}
    assert by["Imported"] == [2, 0]  # the 40-day-old one is placed on the first recorded day
    assert by["New"] == [1, 0] and by["Done"] == [0, 1]
    assert o["per_day"]["imported"] == 2 and o["per_day"]["new"] == 1


def test_claim_to_testing_testing_to_done_and_decisions(ws, put):
    from orch.dashboard.data.flow import overview
    with _pinned_local_tz("UTC"):
        _two_days(ws, put)
        o = overview(ws, days=28, now=NOW)
    assert o["claim_to_testing_hours"] == pytest.approx(1.0) and o["n_claim"] == 1
    to_testing = NOW - timedelta(days=1, hours=2) + timedelta(minutes=70)
    assert o["testing_to_done_hours"] == pytest.approx((NOW - timedelta(hours=1) - to_testing).total_seconds() / 3600)
    assert o["decisions"] == {"gates": 1, "verdicts": 1, "answers": 1, "total": 3}
    claim = o["claim_chart"]
    assert dict(zip(claim["labels"], claim["series"][0]["values"]))["1–2 h"] == 1
    assert sum(claim["series"][0]["values"]) == 1


def test_agent_actions_are_not_your_decisions(ws, put):
    from orch.dashboard.data.flow import overview
    t = put("open")
    _events(ws, [(NOW - timedelta(hours=3), t, "gate.approved", {"gate": "plan"}, "agent:claude"),
                 (NOW - timedelta(hours=2), t, "gate.approved", {"gate": "plan"})])
    with _pinned_local_tz("UTC"):
        assert overview(ws, days=7, now=NOW)["decisions"]["gates"] == 1


def test_a_period_before_exists_only_when_the_record_reaches_back_that_far(ws, put):
    from orch.dashboard.data.flow import overview
    t = put("done")
    _events(ws, [(NOW - timedelta(days=60), t, "ticket.created", {}),
                 (NOW - timedelta(days=1), t, "ticket.moved", {"to": "done"})])
    with _pinned_local_tz("UTC"):
        assert overview(ws, days=28, now=NOW)["has_before"] is True
        assert overview(ws, days=90, now=NOW)["has_before"] is False


def test_needs_you_now_counts_what_waits_on_you(ws, put):
    from orch.dashboard.data.flow import overview
    put("testing", sections={"Verification": "check it"})
    put("in-progress")
    put("in-progress")
    n = overview(ws, now=NOW)["needs"]
    assert n["testing"] == 1 and n["working"] == 2 and n["questions"] == 0


def test_empty_workspace_says_nothing_is_recorded(dash):
    html = dash.get("/reports").text
    assert "Nothing recorded yet" in html and "Needs you now" in html
    assert '<figure class="chart"' not in html and "vs. the period before" not in html
    assert "Nothing recorded yet" in dash.get("/reports.md").text


def test_page_with_two_days_draws_charts_and_names_what_it_measures(dash, ws, put):
    new = put("done")
    now = datetime.now(timezone.utc)
    _events(ws, [(now - timedelta(hours=40), new, "ticket.created", {}),
                 (now - timedelta(hours=39), new, "claim.taken", {}),
                 (now - timedelta(hours=38), new, "ticket.moved", {"from": "in-progress", "to": "testing"}),
                 (now - timedelta(hours=1), new, "ticket.moved", {"from": "testing", "to": "done"})])
    html = dash.get("/reports").text
    assert "orch has recorded this workspace since" in html and "vs. the period before" not in html
    assert "Claim &rarr; testing" in html and "Testing &rarr; done" in html and "Your decisions" in html
    assert "median elapsed over 1 done ticket, waits included" in html
    assert "Median open &rarr; done" in html and "so a long ticket weighs more than a short one" in html
    assert _spec(html, "chart-per-day")["series"][-1]["name"] == "Done"
    assert _spec(html, "chart-claim")["unit"] == "tickets"
    assert html.count('<details class="chart-data">') == 2  # the numbers are in the page, not only the canvas
    assert '<script src="/static/vendor' not in html  # charts.js fetches the library, only on a page with a chart
    md = dash.get("/reports.md").text
    for part in ("## Needs you now", "## Created and done per day", "## Claim -> testing", "median open -> done"):
        assert part in md


def test_one_day_of_record_gives_the_numbers_alone(dash, ws, put):
    t = put("backlog")
    now = datetime.now(timezone.utc)
    _events(ws, [(now - timedelta(minutes=5), t, "ticket.created", {})])
    html = dash.get("/reports").text
    assert "the charts give way to the numbers alone" in html and '<figure class="chart"' not in html
    assert "1 new" in html


def test_pages_without_a_chart_do_not_name_the_library(dash):
    assert "chart.umd" not in dash.get("/board").text
