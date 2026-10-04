"""The widget catalog as a tab of Workspace & addons (issue #54): nav, redirect, GET filters, the detail panel, the
section links, the `orch move` hint and the adoption text in the skills and rendered rules."""
import json
import re
from pathlib import Path

from orch.instructions.render import agents_rules
from orch.widgets import registry

SKILLS = Path(__file__).resolve().parents[1] / "skills"
TAB = "/workspace?tab=widgets"


def tiles(body):
    return re.findall(r'<article class="cat-tile"[^>]* id="w-([a-z-]+)"', body)


def test_nav_has_no_widgets_but_the_tab_does(dash):
    page = dash.get("/").text
    nav = page[page.index('<nav class="menu"'):page.index("</nav>")]
    assert 'href="/widgets"' not in nav and ">Widgets<" not in nav
    ws_page = dash.get("/workspace").text
    assert 'data-tab="widgets"' in ws_page and 'id="tab-widgets"' in ws_page


def test_widgets_redirects_to_the_tab_and_keeps_filters(dash):
    r = dash.get("/widgets?q=diff&g=review", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/workspace?tab=widgets&q=diff&g=review"
    body = dash.get("/widgets?q=diff").text
    assert "diffstat" in tiles(body) and "checks" not in tiles(body)


def test_nine_first_then_more(dash):
    body = dash.get(TAB).text
    names = tiles(body)
    assert names[:9] == ["checks", "screens", "compare", "stats", "options", "callout", "table", "links", "diff"]
    assert f"More types ({len(names) - 9})" in body and len(names) >= 37


def test_task_group_and_search_filters_are_get_params(dash):
    assert tiles(dash.get(TAB + "&task=prove").text) == ["checks", "stats", "links"]
    assert tiles(dash.get(TAB + "&task=warn").text) == ["callout", "table", "diff"]
    review = tiles(dash.get(TAB + "&g=review").text)
    assert "diff" in review and "checks" not in review
    assert tiles(dash.get(TAB + "&q=waterfall").text) == ["agent-waterfall"]
    empty = dash.get(TAB + "&q=zzzz-nothing").text
    assert "No widget matches" in empty and not tiles(empty)


def test_selection_follows_filters(dash):
    body = dash.get(TAB + "&task=ui").text
    side = body[body.index('aria-label="Selected widget"'):]
    assert "<code>screens</code>" in side  # first of the filtered list, not checks
    body = dash.get(TAB + "&task=ui&w=links").text
    assert "<code>links</code>" in body[body.index('aria-label="Selected widget"'):]
    body = dash.get(TAB + "&task=ui&w=checks").text  # not in the filter: falls back to the first
    assert "<code>screens</code>" in body[body.index('aria-label="Selected widget"'):]


def test_detail_panel_example_is_valid_and_copyable(dash, ws):
    body = dash.get(TAB + "&w=checks").text
    side = body[body.index('aria-label="Selected widget"'):]
    assert "Core type: draws on every ticket" in side
    m = re.search(r'data-copy="(```orch\n.*?\n```)"', side, re.S)
    assert m
    import html
    fence = html.unescape(m[1])
    data = json.loads(fence.split("\n", 1)[1].rsplit("\n```", 1)[0])
    assert data == registry.core_types()["checks"].EXAMPLE
    assert "Fields (" in side and "Not used on any ticket yet." in side
    tmpl = dash.get(TAB + "&w=mermaid").text
    tmpl_side = tmpl[tmpl.index('aria-label="Selected widget"'):]
    assert "Template:" in tmpl_side and "orch widget add" in tmpl_side


def test_section_links_on_a_ticket(dash, put):
    tid = put("in-progress", sections={"Context": "Because.", "Findings": "Found."})
    body = dash.get(f"/t/{tid}").text
    assert 'href="/workspace?tab=widgets&amp;section=Context"' in body
    assert 'href="/workspace?tab=widgets&amp;section=Findings"' in body
    assert 'href="/workspace?tab=widgets&amp;section=Verification"' in body
    assert tiles(dash.get(TAB + "&section=Verification").text)[:3] == ["checks", "screens", "compare"]


def test_move_to_testing_hints_at_a_verification_widget(ws, put, aops):
    from orch.core import store
    t = put("in-progress")
    ops = aops
    tk = store.load(ws, t)[1]
    hints = ops._handover_warnings(tk)
    assert any("Verification holds no widget" in h and "--type checks" in h for h in hints)
    tk.set_section("Verification", '```orch\n{"type": "text", "text": "x"}\n```')
    assert not any("Verification holds no widget" in h for h in ops._handover_warnings(tk))


def test_skills_and_rules_tell_agents_when_to_use_widgets():
    work = (SKILLS / "orch-work-on-ticket" / "SKILL.md").read_text(encoding="utf-8")
    for needle in ("`checks` widget, one row per acceptance criterion", "`screens`", "`compare`", "`stats`", "`options`",
                   "orch widget add"):
        assert needle in work, needle
    tickets = (SKILLS / "orch-tickets" / "SKILL.md").read_text(encoding="utf-8")
    assert "otherwise write prose" not in tickets and "Default to one for Verification" in tickets
    from orch.config.load import DEFAULTS, deep_merge
    rules = agents_rules(deep_merge(DEFAULTS, {"customer": "acme"}))
    assert "put a `checks` widget in Verification" in rules and "`options` when you ask the human to choose" in rules
