"""The Board's Epics view: an epic and its tickets in a lane are one group card; "All tickets" is the flat board."""
import re

import pytest

pytest.importorskip("fastapi")

from orch.dashboard.data import epic as epic_data  # noqa: E402

NEEDS = {"Requirements": "r", "Acceptance criteria": "- [ ] a"}  # an open ticket whose requirements wait for you


def _lane(html: str, name: str) -> str:
    m = re.search(rf'<section class="col lane[^"]*" id="col-{name}".*?</section>', html, re.S)
    assert m, f"no lane {name}"
    return m.group(0)


def _count(lane: str) -> str:
    return re.search(r'<span class="count">(\d+)</span>(?: <span class="count-sub">([^<]+)</span>)?', lane).groups()


@pytest.fixture
def epic(put):
    eid = put("open", title="Class trips", type="epic")
    kids = [put("open", title=f"Kid {i}", parent=eid) for i in range(3)]
    return eid, kids


def test_children_are_one_group_card_per_lane(dash, epic):
    eid, kids = epic
    lane = _lane(dash.get("/board").text, "open")
    assert lane.count('data-group="') == 1 and f'data-group="{eid}:open"' in lane
    assert re.search(rf'<a class="group-head" href="/t/{eid}">', lane)
    assert "Children 0/3" in lane and "3 tickets here" in lane
    assert _count(lane) == ("1", "3 tickets")  # one card, three tickets
    for k in kids:  # still there, inside the group, draggable as before
        assert f'data-ticket="{k}"' in lane
    assert lane.count('class="card tcard group-card"') == 1 and "draggable" not in lane.split("group-foot")[0]


def test_the_epic_is_a_container_not_a_card(dash, epic):
    eid, _ = epic
    assert f'data-ticket="{eid}"' not in dash.get("/board").text


def test_group_foot_is_a_button_outside_the_link(dash, epic):
    lane = _lane(dash.get("/board").text, "open")
    head = lane[lane.index('<a class="group-head"'):lane.index("</a>", lane.index('<a class="group-head"'))]
    assert "<button" not in head
    assert re.search(r'<button type="button" class="group-toggle" data-group-toggle aria-expanded="false" '
                     r'aria-controls="(grp-[^"]+-k)"', lane)
    assert 'aria-labelledby="grp-' in lane and "Show 3 tickets" in lane


def test_a_group_opens_by_itself_when_a_child_needs_you(dash, aops, hops, put):
    eid = aops.new("Epic", type="epic").id
    t = aops.new("Needs me", epic=eid)
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    aops.claim(t.id)
    aops.set_section(t.id, "Plan", "1. do it")  # the plan now waits on you
    put("in-progress", title="Plain", parent=eid)
    lane = _lane(dash.get("/board").text, "in-progress")
    assert 'aria-expanded="true"' in lane and "Hide 2 tickets" in lane and "1 need you" in lane
    assert re.search(r'<div class="group-kids" id="[^"]+">', lane)  # not hidden


def test_all_tickets_is_the_flat_board(dash, epic):
    eid, kids = epic
    flat = dash.get("/board?show=all").text
    lane = _lane(flat, "open")
    assert "data-group" not in flat and "group-card" not in lane
    assert _count(lane) == ("4", None)  # epic + three children, no "N tickets" note
    assert all(f'data-ticket="{t}"' in lane for t in [eid, *kids])


def test_epic_without_children_keeps_its_card(dash, put):
    eid = put("open", title="Lonely epic", type="epic")
    html = dash.get("/board").text
    assert f'data-ticket="{eid}"' in _lane(html, "open") and "data-group" not in html


def test_orphan_child_and_non_epic_parent_stay_plain(dash, put):
    orphan = put("open", title="Orphan", parent="B-9999")
    parent = put("open", title="Not an epic")
    child = put("open", title="Follow-up", parent=parent)
    html = dash.get("/board").text
    lane = _lane(html, "open")
    assert "data-group" not in html
    assert all(f'data-ticket="{t}"' in lane for t in (orphan, parent, child))
    assert f"part of {parent}" in lane


def test_epic_with_children_in_two_lanes_is_a_group_in_each(dash, put):
    eid = put("open", title="Spread", type="epic")
    put("open", title="A", parent=eid)
    put("in-progress", title="B", parent=eid)
    html = dash.get("/board").text
    assert f'data-group="{eid}:open"' in _lane(html, "open")
    assert f'data-group="{eid}:in-progress"' in _lane(html, "in-progress")
    assert f'data-ticket="{eid}"' not in html


def test_counts_cards_in_lanes_and_tickets_in_tabs(dash, epic, put):
    put("open", title="Loose")
    html = dash.get("/board").text
    assert _count(_lane(html, "open")) == ("2", "4 tickets")  # group + loose card; 3 children + loose
    tabs = re.search(r'<nav class="tabs" id="board-tabs".*?</nav>', html, re.S).group(0)
    assert re.findall(r'<span class="count">(\d+)</span>', tabs) == ["5", "5"]  # epic + 3 + loose, as tickets
    assert re.findall(r'<span class="count">(\d+)</span>', dash.get("/board?show=all").text.split('id="board-tabs"')[1][:600])[:2] == ["5", "5"]


def test_filters_apply_before_grouping(dash, put):
    eid = put("open", title="Epic", type="epic")
    put("open", title="Big", parent=eid, size="l")
    put("open", title="Small one", parent=eid, size="s")
    put("open", title="Small two", parent=eid, size="s")
    lane = _lane(dash.get("/board?q=").text, "open")
    assert "3 tickets here" in lane
    # a filter that drops a child: the group counts what is left, and the header agrees with what is drawn
    only = dash.get("/board?priority=high").text
    assert "data-group" not in only  # nothing matches: no empty group
    t = put("open", title="High", parent=eid, priority="high")
    lane = _lane(dash.get("/board?priority=high").text, "open")
    assert "1 ticket here" in lane and f'data-ticket="{t}"' in lane and _count(lane)[0] == "1"


def test_search_keeps_the_rows_view(dash, epic):
    html = dash.get("/board?q=Kid").text
    assert "search-results" in html and "data-group" not in html


def test_swimlanes_do_not_use_group_cards(dash, epic):
    html = dash.get("/board?group=epic").text
    assert 'class="lane-h"' in html and "data-group" not in html and "show-by" not in html


def test_the_toggle_is_a_form_field_and_defaults_to_epics(dash, epic):
    html = dash.get("/board").text
    assert re.search(r'name="show" value="epics" checked', html)
    assert re.search(r'name="show" value="all" checked', dash.get("/board?show=all").text)


def test_more_than_five_tickets_offer_show_all(dash, put):
    eid = put("open", title="Big epic", type="epic")
    for i in range(7):
        put("open", title=f"T{i}", parent=eid)
    lane = _lane(dash.get("/board").text, "open")
    assert 'data-group-more' in lane and "Show all 7" in lane and lane.count('class="group-extra"') == 1
    extra = lane[lane.index('class="group-extra"'):]
    assert extra.count('data-ticket="') == 2


def _card(id_, epic_id, who="agent", agent=None, what="ready"):
    return {"id": id_, "move": {"who": who, "what": what}, "epic": {"id": epic_id, "title": "E", "status": "open"},
            "rollup": None, "agent": agent}


def test_no_progress_counts_stale_and_claimed_without_activity():
    cols = {"in-progress": [_card("B-1", "B-9", agent={"status": "stale", "last": None}),
                            _card("B-2", "B-9", agent={"status": "working", "last": None}),
                            _card("B-3", "B-9", agent={"status": "working", "last": "now"}),
                            _card("B-4", "B-9", what="stale")]}
    lanes = epic_data.board_lanes(cols, ["in-progress"], {"B-9": {"id": "B-9", "title": "E", "status": "open"}},
                                  grouped=True)
    g = lanes["in-progress"]["entries"][0]["group"]
    assert g["idle"] == 3 and g["need"] == 0 and lanes["in-progress"]["cards"] == 1 and lanes["in-progress"]["tickets"] == 4
