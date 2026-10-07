import re

import pytest

pytest.importorskip("fastapi")


def _approved_open(put, hops, title, **sections):
    """A ticket whose requirements the human approved for real (Ops.approve moves it to open)."""
    tid = put("backlog", title=title, sections={"Requirements": "r", "Acceptance criteria": "a", **sections})
    hops.approve(tid, "requirements")
    return tid


def test_today_headline(dash, put):
    put("testing", sections={"Verification": "ok"})
    html = dash.get("/").text
    assert "<h1>1 decision, then the agents run on their own</h1>" in html  # M: one headline instead of tiles


def test_today_in_flight_lists_agent_turn(dash, put, hops):
    _approved_open(put, hops, "Agent should start")
    html = dash.get("/").text
    assert "<details" in html and "In flight" in html and "Agent should start" in html


def test_title_carries_needs_count(dash, put):
    put("testing", sections={"Verification": "ok"})
    assert "<title>(1) Today" in dash.get("/").text


def test_today_empty_state(dash):
    html = dash.get("/").text
    assert "Nothing is waiting on you" in html and "<title>Today" in html and "(0)" not in html


def test_waiting_status_label(dash, put):
    put("waiting", title="Blocked elsewhere")
    # waiting = a blocking question waits on the human: the `you` role (design-system spec D6)
    html = dash.get("/?inflight=1").text + dash.get("/").text
    assert 'class="chip chip-you"><svg class="i" aria-hidden="true"><use href="#i-you"/></svg> Waiting<' in html


# ---------- beyond the brief: the data helpers and the In flight details ----------

def test_in_flight_skips_needs_you_and_backlog(ws, put, hops):
    from orch.core import events as events_mod
    from orch.core import query, store
    from orch.dashboard.data import today

    agent_turn = _approved_open(put, hops, "Agent turn", **{"Current state": "Plan drafted\nsecond line"})
    verdict = put("testing", sections={"Verification": "ok"})
    put("backlog", title="Still in backlog")
    put("done", title="Finished")
    entries = store.scan(ws)
    needs = query.needs_you(ws, entries=entries)
    cards = today.in_flight(ws, needs=needs, entries=entries, events=events_mod.read_events(ws))
    assert [c["id"] for c in cards] == [agent_turn]
    card = cards[0]
    assert card["status"] == "open" and card["title"] == "Agent turn"
    assert card["next"] == "Plan drafted"
    assert [s["name"] for s in card["steps"]][0] == "Requirements" and card["steps"][0]["state"] == "done"
    assert card["agent"] is None and card["start"] and card["start"]["key"] == agent_turn
    assert verdict not in [c["id"] for c in cards]


def test_summary_counts_tickets_oldest_and_working(ws, put):
    from orch.clock import stamp
    from orch.core import query, store
    from orch.dashboard.data import agents, today

    put("testing", sections={"Verification": "ok"})
    put("in-progress", claim={"harness": "claude-code", "session": "s1", "at": stamp()})
    entries = store.scan(ws)
    needs = query.needs_you(ws, entries=entries)
    rows = agents.agent_rows(ws, entries=entries, needs=needs)
    s = today.summary(ws, needs=needs, rows=rows)
    assert s["waiting"] == 1 and s["working"] == 1
    assert "oldest_minutes" in s


def test_in_flight_details_closed_and_remembered(dash, put, hops):
    _approved_open(put, hops, "Agent should start")
    html = dash.get("/").text
    tag = re.search(r"<details[^>]*in-flight[^>]*>", html).group(0)
    assert " open" not in tag and "data-remember=" in tag


def test_working_agent_shows_chip_not_start_box(dash, put, hops, ws):
    from orch.clock import stamp
    from orch.core import store
    tid = _approved_open(put, hops, "Being worked on")
    _, t = store.load(ws, tid)
    t.meta["claim"] = {"harness": "copilot", "session": "s9", "at": stamp()}
    store.save(ws, t)
    html = dash.get("/").text
    assert "copilot · working" in html and f'id="start-agent-{tid}"' not in html


def test_agent_turn_card_has_start_agent_box(dash, put, hops):
    tid = _approved_open(put, hops, "Agent should start")
    html = dash.get("/").text
    assert f'id="start-agent-{tid}"' in html and 'name="next" value="/"' in html


def test_create_first_ticket_only_without_tickets(dash, put):
    assert "Create first ticket" in dash.get("/").text
    put("waiting", title="Blocked elsewhere")
    html = dash.get("/").text
    assert "Nothing is waiting on you" in html and "Create first ticket" not in html


def test_one_ticket_two_items_same_count_everywhere(dash, put, aops):
    """A testing ticket with a blocking question: two needs-you items on one ticket. The tab
    title, the menu badge, the strip and the heading all show the item count."""
    tid = put("testing", sections={"Verification": "ok"})
    aops.ask(tid, [{"text": "Which schema?", "type": "text", "blocking": True}])
    html = dash.get("/").text
    assert "<title>(2) Today" in html
    assert re.search(r'class="badge badge-hot" aria-label="2 decisions wait on you">2</span>', html)
    assert "<h1>2 decisions, then the agents run on their own</h1>" in html


# ---------- a changed gate that cannot be approved in this status (final review I1) ----------

def _edit(ws, tid, approve=(), **sections):
    from orch.core import store
    from orch.core.gates import gate_hash
    _, t = store.load(ws, tid)
    for g in approve:
        t.meta["gates"][g] = {"approved": "2026-09-30T09:00Z", "via": "dashboard", "hash": gate_hash(t, g)}
    for name, text in sections.items():
        t.set_section(name.replace("_", " "), text)
    store.save(ws, t)


def _card(html, tid):
    start = html.index(f'href="/t/{tid}" data-open>{tid}</a>')
    head = html.rindex('<article class="decision-card decision', 0, start)
    return html[head:html.index("</article>", start)]


def test_today_reapprove_outside_status_offers_move_back(dash, ws, put):
    tid = put("in-progress", sections={"Requirements": "- r", "Acceptance criteria": "- a"})
    _edit(ws, tid, approve=("requirements",))
    _edit(ws, tid, Requirements="- r changed")
    html = dash.get("/").text
    card = _card(html, tid)
    assert f'action="/t/{tid}/approve"' not in html
    assert "move the ticket back to backlog" in card
    assert f'action="/t/{tid}/move"' in card and 'name="to" value="backlog"' in card and "Move back to backlog" in card
    assert 'data-confirm-ok="Move to backlog"' in card
    assert f'action="/t/{tid}/request-changes"' in card
    assert "<h1>1 decision, then the agents run on their own</h1>" in html  # the item stays in needs-you


def test_today_changed_plan_in_testing_has_no_approve(dash, ws, put):
    tid = put("testing", sections={"Requirements": "- r", "Acceptance criteria": "- a", "Plan": "- [ ] a",
                                   "Verification": "ok"})
    _edit(ws, tid, approve=("requirements", "plan"))
    _edit(ws, tid, Plan="- [ ] a\n- [ ] new step")
    html = dash.get("/").text
    assert f'action="/t/{tid}/approve"' not in html
    assert "Plan changed after approval" in html and f'action="/t/{tid}/request-changes"' in html
