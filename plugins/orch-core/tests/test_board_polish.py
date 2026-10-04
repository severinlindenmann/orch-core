"""F: Board polish (visual review top-10 #5): Done as a narrow rail, status labels with their icon in the column
headers, the page-head wrapper, visible filter labels with the lesser filters behind "More filters", and a phone
layout that stacks the columns."""
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

CSS = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard" / "static" / "app.css"


def test_done_is_a_rail_until_done_tickets_are_shown(dash, put):
    # M: Done is a rail with this week's count that opens the List filtered on done; "show done" makes it a lane
    put("done", title="Old work")
    html = dash.get("/board").text
    rail = re.search(r'<a class="lane lane-rail rail-done" href="([^"]+)" data-status="done" aria-label="1 done in the last 7 days', html)
    assert rail and "status=done" in rail.group(1) and "view=list" in rail.group(1)
    listing = dash.get(rail.group(1).replace("&amp;", "&")).text
    assert "Old work" in listing
    shown = dash.get("/board?show_done=1").text
    assert re.search(r'<section class="col lane flow-lane" id="col-done"', shown) and "rail-done" not in shown


def test_column_headers_use_flow_words_and_role_icons(dash, put):
    put("waiting"); put("in-progress")
    html = dash.get("/board").text
    heads = re.findall(r'<h2 class="col-h col-(\w+)"><svg class="i" aria-hidden="true"><use href="#i-(\w+)"/></svg> ([^<]+) <span', html)
    assert [h[2] for h in heads] == ["Working", "Waiting", "Backlog"]  # Ready and Testing are empty: rails
    assert all(role == icon for role, icon, _ in heads)
    assert ("neu", "neu", "Waiting") in heads  # never pink: the move chip is the only pink
    assert 'aria-label="Ready: no tickets"' in html and 'aria-label="Testing: no tickets"' in html
    assert "IN-PROGRESS" not in html and ">in-progress <" not in html


def test_page_head_wraps_title_and_subline(dash):
    html = dash.get("/board").text
    assert re.search(r'<div class="page-head">\s*<div>\s*<h1>Board</h1>\s*<p>Your move on top', html)


def test_lesser_filters_sit_behind_more_filters(dash):
    html = dash.get("/board").text
    form = html[html.index('<form class="filters"'):html.index("</form>", html.index('<form class="filters"'))]
    assert "data-autosubmit" in form
    for label in ("Search", "Repository", "Group by", "Type", "Priority", "External key", "Label"):
        assert f'<span class="filter-label">{label}</span>' in form
    assert '<details class="more-filters">' in form  # closed while none of them is set
    assert form.index('name="repo"') < form.index("more-filters") < form.index('name="type"')
    assert ">Any type<" in form and ">Any priority<" in form and "any type" not in form
    opened = dash.get("/board?priority=high").text
    assert '<details class="more-filters" open>' in opened and 'aria-label="1 set"' in opened


def test_phone_stacks_the_columns_and_cards_keep_words_whole():
    css = CSS.read_text(encoding="utf-8")
    phone = css[css.index("@media (max-width: 480px)"):]
    assert re.search(r"\.board, \.board\.board-rail \{[^}]*flex-direction: column", phone)
    assert re.search(r"\.board \.tcard \.title, \.flow \.tcard \.title \{[^}]*overflow-wrap: break-word", css)
    assert "overflow-wrap: anywhere; }" not in css[css.index(".board .tcard .title"):][:200]
    assert re.search(r"\.tcard \.tc-agent \{[^}]*white-space: nowrap", css)


def test_board_scroller_contains_its_hidden_texts():
    """The cards' .sr-only spans are absolutely positioned: the board must be their containing block, or they widen
    the whole page (the 1024 px and phone sideways scroll)."""
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"^\.board \{ position: relative;", css, re.M)


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_filters_autosubmit_only_on_pointer_input_and_busy_timer_stops():
    import subprocess
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(["node", str(root / "tests" / "js" / "board_filters.js"), str(root / "src/orch/dashboard/static/app.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "board filters ok" in r.stdout


def test_filter_button_stays_visible_and_more_filters_has_a_chevron(dash):
    css = CSS.read_text(encoding="utf-8")
    assert "filter-apply { display: none" not in css
    assert '<button type="submit" class="btn filter-apply">Filter</button>' in dash.get("/board").text
    assert re.search(r'\.more-filters > summary::after \{ content: "▸"', css)
    assert re.search(r"\.board \{ grid-template-columns: repeat\(6, minmax\(150px, 1fr\)\)", css)
