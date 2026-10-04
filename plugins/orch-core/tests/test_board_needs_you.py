"""F1/F4/C: the Board's first lane is "Needs you" (every ticket whose move is the human's, across statuses, the only
pink), the other lanes show the agents' side, every Needs-you card says in one line why it waits, and the Backlog
starts folded to a count (remembered per user), sorted by priority then age, agent ideas marked."""
import re

import pytest

pytest.importorskip("fastapi")


def _lane(html: str, name: str) -> str:
    m = re.search(rf'<section class="col lane[^"]*" id="col-{name}".*?</section>', html, re.S)
    assert m, f"no lane {name}"
    return m.group(0)


@pytest.fixture
def needs_requirements(aops):
    t = aops.new("Approve me")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    return t.id


def _strip(html: str) -> str:
    m = re.search(r'<section class="ym-strip" id="your-move".*?(?=<div class="flow"|<section class="swimlane")', html, re.S)
    assert m, "no Your move strip"
    return m.group(0)


# M (v4) replaced F1's Needs you lane with the Your move strip on top: the decisions stand there (actionable), and the
# cards stay in their flow lanes too, so the flow is complete. These tests were rewritten for that on purpose.

def test_your_move_is_on_top_and_the_flow_lanes_follow(dash, needs_requirements, put):
    put("open", title="Ready"); put("in-progress", title="Busy")
    html = dash.get("/board").text
    assert html.index('id="your-move"') < html.index('class="flow"')
    lanes = re.findall(r'<section class="col lane[^"]*" id="col-([a-z-]+)"', html)
    assert lanes == ["open", "in-progress", "waiting", "testing", "backlog"]
    assert "Your move" in _strip(html)


def test_a_ticket_whose_move_is_yours_is_in_the_strip_and_its_lane(dash, needs_requirements):
    html = dash.get("/board").text
    assert f'data-ym="{needs_requirements}"' in _strip(html)
    assert f'data-ticket="{needs_requirements}"' in _lane(html, "backlog")


def test_your_move_spans_statuses(dash, put, working, aops):
    tid = put("testing", title="Check me")
    aops.set_section(working, "Plan", "1. do it")
    strip = _strip(dash.get("/board").text)
    assert f'data-ym="{tid}"' in strip and f'data-ym="{working}"' in strip


def test_the_agents_side_is_not_in_the_strip(dash, put):
    tid = put("open", title="Ready for an agent")
    html = dash.get("/board").text
    assert f'data-ticket="{tid}"' in _lane(html, "open") and tid not in _strip(html)


def test_pink_only_on_moves_that_are_yours(dash, needs_requirements, put):
    ready = put("open", title="Ready")
    html = dash.get("/board").text
    assert "chip-you" in _strip(html)
    card = re.search(rf'<a class="card tcard" href="/t/{ready}".*?</a>', html, re.S).group(0)
    assert "chip-you" not in card


def test_every_strip_card_says_why(dash, needs_requirements, put):
    put("testing", title="Check me")
    strip = _strip(dash.get("/board").text)
    assert "nobody starts until you approve them" in strip
    assert "Check the proof" in strip  # a verdict opens its full proof before Accept


def test_an_empty_strip_says_so(dash, put):
    put("open", title="Ready")
    assert "Nothing needs you" in _strip(dash.get("/board").text)


def test_today_cards_say_why_they_wait(dash, working, aops):
    aops.set_section(working, "Plan", "1. do it")
    html = dash.get("/").text
    assert "it starts once you approve the plan" in html


# -- C: the backlog folds -----------------------------------------------------------------------------------------

def test_backlog_is_folded_to_a_count_by_default(dash, put):
    for i in range(3):
        put("backlog", title=f"Idea {i}")
    backlog = _lane(dash.get("/board").text, "backlog")
    fold = re.search(r"<details class=\"col-fold\"([^>]*)>", backlog)
    assert fold and "open" not in fold.group(1)
    assert re.search(r"Backlog <span class=\"count\">3</span>", backlog)
    assert "Idea 0" in backlog  # expandable inline: the cards are in the page, folded


def test_backlog_is_sorted_by_priority_then_age(dash, put):
    a = put("backlog", title="Old normal", priority="normal", created="2026-01-01T09:00Z")
    b = put("backlog", title="New urgent", priority="urgent", created="2026-03-01T09:00Z")
    c = put("backlog", title="New normal", priority="normal", created="2026-02-01T09:00Z")
    backlog = _lane(dash.get("/board").text, "backlog")
    order = [backlog.index(f'data-ticket="{x}"') for x in (b, a, c)]
    assert order == sorted(order)


def test_the_open_backlog_is_remembered(dash, put, ws):
    put("backlog", title="Idea")
    assert "open" in re.search(r"<details class=\"col-fold\"([^>]*)>",
                               _lane(dash.get("/board?backlog=open").text, "backlog")).group(1)
    again = _lane(dash.get("/board").text, "backlog")
    assert "open" in re.search(r"<details class=\"col-fold\"([^>]*)>", again).group(1)
    r = dash.post("/board/backlog", data={"open": "0"}, follow_redirects=False)
    assert r.status_code == 303
    closed = _lane(dash.get("/board").text, "backlog")
    assert "open" not in re.search(r"<details class=\"col-fold\"([^>]*)>", closed).group(1)


def test_the_backlog_preference_is_per_user_not_in_the_repository(dash, put, ws):
    put("backlog", title="Idea")
    dash.get("/board?backlog=open")
    from orch.dashboard import prefs
    assert prefs.prefs_path().is_file() and not str(prefs.prefs_path()).startswith(str(ws.root))


def test_agent_ideas_are_marked(dash, aops, hops):
    by_agent = aops.new("Agent's idea")
    by_you = hops.new("My idea")
    backlog = _lane(dash.get("/board").text, "backlog")
    card = lambda tid: re.search(rf'<a class="card tcard" href="/t/{tid}".*?</a>', backlog, re.S).group(0)
    assert "agent idea" in card(by_agent.id) and "agent idea" not in card(by_you.id)


def test_backlog_tickets_that_need_you_are_not_folded_away(dash, needs_requirements, put):
    put("backlog", title="Idea")
    html = dash.get("/board").text
    assert f'data-ym="{needs_requirements}"' in _strip(html)  # on top, never folded away
    assert re.search(r"Backlog <span class=\"count\">2</span>", _lane(html, "backlog"))


def test_a_search_never_hides_a_matching_backlog_ticket_that_needs_you(dash, needs_requirements, put):
    put("backlog", title="Approve me later, idea")
    for view in ("", "&view=list", "&group=label"):
        results = dash.get("/board?q=approve" + view).text.split('id="board-results"', 1)[1]
        assert f'href="/t/{needs_requirements}"' in results, view
        assert '<details class="col-fold">' not in results, view  # nothing folded while searching
    dash.get("/board?group=none")
