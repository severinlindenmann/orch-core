"""F: Activity polish (visual review top-10 #9): runs of the same event fold into one line, a smaller page,
the category as quiet text, ticket keys with their title, and Release claim as a quiet in-page-dialog action that
never clips."""
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

CSS = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard" / "static" / "app.css"


def test_runs_of_the_same_event_fold(ws, hops):
    from orch.dashboard.data.timeline import timeline
    tickets = [hops.new(f"T{i}") for i in range(4)]  # four "created the ticket" in a row by the same actor
    hops.log(tickets[0].id, "one line")
    entries = timeline(ws).groups[0]["entries"]
    assert [e["run"] for e in entries] == [False, True]
    run = entries[1]
    assert run["count"] == 4 and run["who"] == "you" and run["what"] == "created the ticket"
    assert len(run["tickets"]) == 4 and len(run["items"]) == 4


def test_two_in_a_row_stay_separate_and_mixed_runs_use_the_phrase(ws, hops):
    from orch.dashboard.data.timeline import timeline
    t = hops.new("Edits")
    for section in ("Context", "Requirements", "Acceptance criteria"):
        hops.set_section(t.id, section, "x")
    entries = timeline(ws).groups[0]["entries"]
    run = entries[0]
    assert run["run"] and run["count"] == 3 and run["what"] == "edited the ticket"  # three sections: a fixed phrase
    two = timeline(ws, limit=2).groups[0]["entries"]
    assert [e["run"] for e in two] == [False, False]


def test_activity_page_shows_fifty_and_pages_on(dash, ws, hops):
    t = hops.new("Busy")
    for n in range(60):
        hops.log(t.id, f"n{n}")
    html = dash.get("/activity").text
    assert ">Older</a>" in html
    from orch.dashboard.data.timeline import PAGE_SIZE
    assert PAGE_SIZE == 50


def test_category_is_text_and_tickets_carry_their_title(dash, hops):
    t = hops.new("Downloader crashes on empty files")
    hops.log(t.id, "note")
    html = dash.get("/activity").text
    feed = html[html.index('id="timeline"'):]
    assert '<span class="chip">' not in feed and "—" not in feed
    assert '<span class="tl-cat muted">Agents</span>' in feed
    assert f'<span class="key">{t.id}</span><span class="tl-title"> · Downloader crashes on empty files</span>' in feed
    assert 'title="Downloader crashes on empty files"' in feed
    assert '<li class="tl-run"><details>' not in feed or "times" in feed


def test_release_claim_is_quiet_and_the_action_column_never_clips(dash, put):
    from datetime import timedelta
    from orch.clock import now, stamp
    tid = put("in-progress", claim={"harness": "claude-code", "session": "s1", "at": stamp(now() - timedelta(hours=9))})
    html = dash.get("/activity").text
    form = html.split(f'action="/t/{tid}/release"', 1)[1].split("</form>", 1)[0]
    assert "data-confirm-title=" in html.split(f'action="/t/{tid}/release"', 1)[1][:200]
    assert f'<button class="btn btn-quiet" type="submit" aria-label="Release claim on {tid}">Release…</button>' in form
    assert '<td class="cell-action col-actions">' in html
    assert re.search(r"\.agents-table \.col-actions \{ width: 1%; white-space: nowrap; \}", CSS.read_text(encoding="utf-8"))
