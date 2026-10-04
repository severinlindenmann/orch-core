import time
from datetime import timedelta

import pytest

from orch.clock import now, parse_stamp, stamp


def _claim(put, status="in-progress", minutes_ago=5, **meta):
    at = stamp(now() - timedelta(minutes=minutes_ago))
    return put(status, claim={"session": "s-123456789", "harness": "claude-code", "at": at}, **meta)


def test_no_claims_no_rows(ws, put):
    from orch.dashboard.data.agents import agent_rows
    put("open")
    assert agent_rows(ws) == []


def test_stale_without_events(ws, put):
    from orch.dashboard.data.agents import agent_rows
    tid = _claim(put, minutes_ago=600)
    (row,) = agent_rows(ws)
    assert row.ticket == tid and row.status == "stale" and row.harness == "claude-code"


def test_working_with_recent_claim(ws, put):
    from orch.dashboard.data.agents import agent_rows
    _claim(put, minutes_ago=3)
    assert agent_rows(ws)[0].status == "working"


def test_waiting_when_ticket_needs_human(ws, put):
    from orch.dashboard.data.agents import agent_rows
    _claim(put, status="testing", minutes_ago=600)   # testing → needs a verdict
    assert agent_rows(ws)[0].status == "waiting"


def test_stale_minutes_from_config(ws, put, configure):
    from orch.core.workspace import Workspace
    from orch.dashboard.data.agents import agent_rows
    _claim(put, minutes_ago=30)
    configure(dashboard={"stale_minutes": 10})
    assert agent_rows(Workspace.open(ws.root))[0].status == "stale"


def test_recent_sessions_pairs_claim_and_release(ws, put, aops):
    from orch.dashboard.data.agents import recent_sessions
    tid = put("open")
    aops.claim(tid)
    aops.release(tid)
    (session,) = recent_sessions(ws)
    assert session["ticket"] == tid
    assert session["harness"] == aops.actor.name
    assert session["minutes"] >= 0


def test_agents_page(dash, put):
    _claim(put, minutes_ago=600, title="Migrate jobs")
    html = dash.get("/activity").text
    assert "Migrate jobs" in html and "Stale" in html and "Release claim" in html
    assert 'action="/t/' in html and '/release"' in html


def test_claimed_and_session_times_render_local(dash, put, monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("tzset not available on this platform (e.g. Windows)")
    at = stamp(now() - timedelta(minutes=5))
    tid = put("in-progress", claim={"session": "s-123456789", "harness": "claude-code", "at": at})
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # UTC+14: guaranteed to differ from the UTC hour
    time.tzset()
    try:
        html = dash.get("/activity").text
        expected_hour = parse_stamp(at).astimezone().strftime("%H:%M")
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
    utc_hour = parse_stamp(at).strftime("%H:%M")
    assert expected_hour != utc_hour
    assert expected_hour in html


def test_recent_sessions_pairs_each_release_with_the_latest_take_of_its_session(ws):
    from orch.core.events import Event
    from orch.dashboard.data.agents import recent_sessions
    def ev(seq, kind, tid, session, at):
        return Event(seq, at, tid, kind, "agent:claude-code:x", "cli", {"session": session})
    events = [
        ev(1, "claim.taken", "L-0001", "a", "2026-09-30T08:00:00Z"),
        ev(2, "claim.taken", "L-0001", "b", "2026-09-30T08:30:00Z"),
        ev(3, "claim.released", "L-0001", "a", "2026-09-30T09:00:00Z"),
        ev(4, "claim.taken", "L-0001", "a", "2026-09-30T10:00:00Z"),
        ev(5, "claim.released", "L-0001", "a", "2026-09-30T10:15:00Z"),
        ev(6, "claim.released", "L-0002", "a", "2026-09-30T11:00:00Z"),  # never taken: skipped
    ]
    got = [(s["session"], s["minutes"]) for s in recent_sessions(ws, events=list(reversed(events)))]
    assert got == [("a", 15), ("a", 60)]


def test_action_column_header_has_hidden_text(dash, put):
    from orch.clock import stamp
    put("in-progress", claim={"harness": "claude-code", "session": "s1", "at": stamp()})
    assert '<th scope="col" class="col-actions"><span class="sr-only">Actions</span></th>' in dash.get("/activity").text
