import re

import pytest

pytest.importorskip("fastapi")


def test_menu_order_and_labels(dash):
    html = dash.get("/").text
    nav = html[html.index('<nav class="menu"'):html.index("</nav>")]
    labels = re.findall(r'class="item[^"]*"[^>]*>.*?<span class="label">([^<]+)</span>', nav, re.S)
    assert labels[:4] == ["Today", "Board", "Activity", "Reports"] and "Workspace &amp; addons" in nav
    assert "Graph" not in labels and "Terminals" not in labels  # #167: both are opt-in addons


def test_no_addon_group_without_addons(dash):
    assert "menu-addons" not in dash.get("/").text


@pytest.mark.parametrize("old,new", [("/agents", "/activity"), ("/timeline?category=code", "/activity?category=code#timeline")])
def test_old_urls_redirect(dash, old, new):
    r = dash.get(old, follow_redirects=False)
    assert r.status_code in (301, 303) and r.headers["location"] == new


def test_activity_has_agents_and_timeline(dash, put):
    put("in-progress", claim={"session": "s1", "harness": "claude-code", "at": "2026-10-02T08:00Z"})
    html = dash.get("/activity").text
    assert "Agents now" in html and 'id="timeline"' in html


def test_board_list_view(dash, put):
    put("open", title="Listed one")
    html = dash.get("/board?view=list").text
    assert "<table" in html and "Listed one" in html and 'aria-current="true"' in html


def test_reports_distribution_bar(dash, put):
    put("open"); put("backlog")
    html = dash.get("/reports").text
    assert 'role="img"' in html and "Tickets by status" in html


# --- beyond the brief's six: the contracts around them ---------------------------------------

def test_addon_group_renders_when_addon_nav_given(dash):
    from orch.dashboard.views import TEMPLATES
    html = TEMPLATES.env.get_template("_menu.html").render(
        nav="", needs_count=0, setup_count=0, theme="system", brand="none", customer="c", prefix="P",
        repo_count=0, addon_nav=[("Code reviews", "/addons/reviews", "M0 0", "", None)])
    assert "menu-addons" in html and 'href="/addons/reviews"' in html and "Code reviews" in html


def test_graph_and_terminals_go_under_addons_when_on():
    from orch.dashboard.views import TEMPLATES
    html = TEMPLATES.env.get_template("_menu.html").render(
        nav="", needs_count=0, setup_count=0, theme="system", brand="none", customer="c", prefix="P",
        repo_count=0, addon_nav=[], graph_nav=True, terminals_nav=True)
    before, group = html.split('class="menu-addons"')
    group = group.split("</div>")[0]
    assert group.index('href="/graph"') < group.index('href="/terminals"')
    assert 'href="/graph"' not in before and 'href="/terminals"' not in before


def test_agents_redirect_keeps_query(dash):
    r = dash.get("/agents?status=stale", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/activity?status=stale"


def test_timeline_markdown_moves_to_activity(dash, hops):
    hops.new("Exported ticket")
    md = dash.get("/activity.md?category=all")
    assert md.headers["content-type"].startswith("text/markdown") and "created the ticket" in md.text
    r = dash.get("/timeline.md?category=code", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/activity.md?category=code"


def test_activity_filters_and_menu_state(dash, put):
    put("in-progress", claim={"session": "s1", "harness": "claude-code", "at": "2026-10-02T08:00Z"})
    html = dash.get("/activity?category=code").text
    assert 'href="/activity.md?category=code"' in html and "Release claim" in html
    current = re.findall(r'<a[^>]*aria-current="page"[^>]*>(.*?)</a>', html, re.S)
    assert len(current) == 1 and "Activity" in current[0]


def test_status_distribution(ws, put):
    from orch.dashboard.data.metrics import status_distribution
    put("open"); put("open"); put("done")
    dist = {d["name"]: d for d in status_distribution(ws)}
    assert dist["open"]["n"] == 2 and dist["done"]["n"] == 1 and dist["done"]["role"] == "ok"
    assert "backlog" not in dist  # zero segments are left out


def test_distribution_segments_link_to_board(dash, put):
    put("open")
    html = dash.get("/reports").text
    assert 'href="/board#col-open"' in html and "Open 1" in html


def test_reports_empty_workspace_has_no_distribution_chart(dash):
    html = dash.get("/reports").text
    assert "Tickets by status" in html and "No tickets yet" in html


def test_weekly_bars_zero_baseline_and_partial_week(ws):
    from orch.dashboard.data.metrics import report
    weeks = report(ws)["weeks"]
    assert len(weeks) == 8 and all(w["h"] == "0%" for w in weeks)
    assert weeks[-1]["partial"] and not any(w["partial"] for w in weeks[:-1])


def test_board_tabs_and_list_sort(dash, put):
    put("open", title="Bravo"); put("open", title="Alpha")
    board = dash.get("/board").text
    assert 'href="/board?view=list"' in board and 'class="flow"' in board  # M: the agent flow
    html = dash.get("/board?view=list&sort=title").text
    assert html.index("Alpha") < html.index("Bravo")
    assert 'aria-sort="ascending"' in html


def test_board_list_keeps_filters_and_hides_done(dash, put):
    put("open", title="Bug one", type="bug"); put("open", title="Feature one", type="feature")
    put("done", title="Finished one")
    html = dash.get("/board?view=list&type=bug").text
    assert "Bug one" in html and "Feature one" not in html and "Finished one" not in html
    assert "Finished one" in dash.get("/board?view=list&show_done=1").text


def test_workspace_title_and_agent_start(dash):
    html = dash.get("/workspace").text
    assert "<h1>Workspace &amp; addons</h1>" in html and "Start agent" in html
    assert "Agents only start when you start them" in html


@pytest.mark.parametrize("column", ["key", "title", "status", "priority", "type", "size", "agent", "external", "updated"])
def test_board_list_sorts_hand_edited_values(dash, put, column):
    put("open", title="Odd", priority=["high"], size={"x": 1}, type=3)
    put("open", title="Plain")
    for direction in ("asc", "desc"):
        r = dash.get(f"/board?view=list&sort={column}&dir={direction}")
        assert r.status_code == 200 and "Odd" in r.text and "Plain" in r.text


def test_board_tabs_keep_show_done_and_list_sort(dash, put):
    put("open", title="One")
    html = dash.get("/board?view=list&sort=title&dir=desc&show_done=1&type=bug").text
    assert 'href="/board?type=bug&amp;show_done=1&amp;sort=title&amp;dir=desc"' in html
    board = dash.get("/board?type=bug&show_done=1&sort=title&dir=desc").text
    assert 'href="/board?type=bug&amp;view=list&amp;show_done=1&amp;sort=title&amp;dir=desc"' in board
    assert 'name="sort" value="title"' in board  # filtering on the Board keeps the List sort too


def test_default_tab_links_stay_short(dash):
    html = dash.get("/board").text
    assert 'href="/board"' in html and 'href="/board?view=list"' in html


def test_activity_feed_does_not_reuse_the_ticket_timeline_class(dash):
    from pathlib import Path
    html = dash.get("/activity").text
    assert '<section id="timeline" class="activity-feed"' in html
    css = (Path(__file__).parents[1] / "src" / "orch" / "dashboard" / "static" / "app.css").read_text(encoding="utf-8")
    assert not re.search(r"^\.timeline (>|\.card)", css, re.M)
    assert re.search(r"^\.timeline \{ list-style: none;", css, re.M)  # the ticket page list keeps its rule
