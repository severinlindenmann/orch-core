"""How core draws addon widgets (design-system spec §3.1): callouts without a chip, role cards, flat tables that
stack by column count with ARIA roles, ticket keys linked in widget text, quiet actions in one cluster, Tabs, Time."""
import re
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from orch.addons.widgets import (KV, Action, Badge, Callout, Card, Link, Table, Tabs, Text, Time)  # noqa: E402


def render(*widgets, prefix="DEMO"):
    from orch.dashboard.views import TEMPLATES
    g = SimpleNamespace(addon="hello-status", widgets=widgets, confirms={}, uploads={}, key_prefix=prefix)
    return TEMPLATES.env.from_string('{% import "_widgets.html" as w %}{{ w.widgets(g.widgets, g) }}').render(g=g)


def test_callout_has_a_title_line_not_a_chip():
    html = render(Callout("warn", "Login needed", "gh has no access to acme/infra."))
    assert '<div class="callout callout-warn" role="status">' in html
    assert '<p class="callout-title"><svg class="i" aria-hidden="true"><use href="#i-warn"/></svg> Login needed</p>' in html
    assert "<p>gh has no access to acme/infra.</p>" in html
    assert "chip" not in html
    assert 'role="alert"' in render(Callout("err", "Sync failed"))


def test_role_card_and_linked_card_title():
    html = render(Card("Failed runs", (Badge("err", "2 failed"),), role="err", href="/addons/x/"))
    assert '<section class="card addon-card card-err">' in html
    assert re.search(r'<h2><a class="lnk card-link" href="/addons/x/">Failed runs<span class="chev" aria-hidden="true">›</span></a></h2>', html)


def test_table_has_aria_roles_column_count_and_key_column():
    html = render(Table(("Run", "State", "Took"), (("r1", Badge("ok", "passed"), 12), ("r2", None, 3.5))))
    assert '<table class="table wtable cols-s" data-cols="3" role="table">' in html
    assert '<thead role="rowgroup"><tr role="row"><th scope="col" role="columnheader" class="cell-key">Run</th>' in html
    assert '<tbody role="rowgroup"><tr role="row"><td role="cell" class="cell-key" data-label="Run">r1</td>' in html
    assert '<td role="cell" class="no-prefix" data-label="State"><span class="chip chip-ok">' in html
    assert '<td role="cell" class="num" data-label="Took">12</td>' in html
    assert '<td role="cell" class="cell-empty" data-label="State">' in html


@pytest.mark.parametrize("cols, size", [(1, "s"), (3, "s"), (4, "m"), (5, "m"), (6, "l"), (8, "l"), (9, "xl")])
def test_table_size_class_follows_the_column_count(cols, size):
    names = tuple(f"C{i}" for i in range(cols))
    html = render(Table(names, (tuple("x" for _ in names),)))
    assert f'class="table wtable cols-{size}" data-cols="{cols}"' in html


def test_table_key_column_can_move():
    html = render(Table(("State", "Repo"), (("open", "a/b"),), key=1))
    assert '<td role="cell" class="cell-key" data-label="Repo">a/b</td>' in html


def test_empty_table_row_keeps_the_roles():
    html = render(Table(("A", "B"), (), empty="No runs failed in the last day."))
    assert '<tr role="row"><td role="cell" colspan="2" class="empty">No runs failed in the last day.</td></tr>' in html


def test_ticket_keys_are_linked_in_text_and_cells_after_escaping():
    html = render(Text("<b>See DEMO-0004</b> and demo-12, not GH-1"),
                  Table(("Ticket",), (("DEMO-0019",), (Text("blocks DEMO-0001"),))))
    assert '&lt;b&gt;See <a class="lnk key" href="/t/DEMO-0004">DEMO-0004</a>&lt;/b&gt;' in html
    assert '<a class="lnk key" href="/t/DEMO-12">demo-12</a>' in html
    assert "GH-1</p>" in html and 'href="/t/GH-1"' not in html
    assert '<a class="lnk key" href="/t/DEMO-0019">DEMO-0019</a>' in html
    assert 'blocks <a class="lnk key" href="/t/DEMO-0001">DEMO-0001</a>' in html


def test_no_prefix_means_no_links():
    assert "<a" not in render(Text("DEMO-1"), prefix="")


def test_consecutive_actions_share_one_cluster_and_quiet_is_quiet():
    html = render(Text("x"), Action("rerun", "Rerun checks"), Action("ignore", "Ignore", quiet=True), Text("y"))
    assert html.count('<div class="cluster widget-actions">') == 1
    cluster = html.split('<div class="cluster widget-actions">', 1)[1].split("</div>", 1)[0]
    assert "Rerun checks" in cluster and "Ignore" in cluster
    assert '<button type="submit" class="btn btn-quiet">Ignore</button>' in html
    assert '<button type="submit" class="btn">Rerun checks</button>' in html


def test_tabs_render_as_core_tab_pills():
    html = render(Tabs((Link("Files", "/addons/x/", current=True), Link("Messages", "/addons/x/?v=m")), label="View"))
    assert '<nav class="widget-tabs" aria-label="View"><ul class="tabs">' in html
    assert '<li><a class="on" href="/addons/x/" aria-current="page">Files</a></li>' in html
    assert '<li><a href="/addons/x/?v=m">Messages</a></li>' in html


def test_time_renders_a_time_element_with_the_full_time_as_title():
    html = render(Time("2026-10-03T12:32:00Z"), KV((("Last run", Time("2026-10-03T12:32:00Z", style="at")),)))
    assert re.search(r'<time datetime="2026-10-03T12:32:00Z" title="03\.10\.2026 \d\d:32">[^<]+ ago</time>', html) \
        or re.search(r'<time datetime="2026-10-03T12:32:00Z" title="03\.10\.2026 \d\d:32">(just now|yesterday)</time>', html)
    assert re.search(r'<dd><time datetime="2026-10-03T12:32:00Z" title="03\.10\.2026 \d\d:32">03\.10\. \d\d:32</time></dd>', html)


def test_badge_in_stats_is_a_number_not_a_chip():
    html = render(KV((("Open", 3), ("Failing", Badge("err", "1"))), layout="stats"))
    assert '<dd><span class="stat-badge stat-err"><svg class="i" aria-hidden="true"><use href="#i-err"/></svg> 1</span></dd>' in html


def test_widget_css_rules():
    from pathlib import Path
    css = (Path(__file__).parents[1] / "src/orch/dashboard/static/app.css").read_text(encoding="utf-8")
    assert re.search(r"\.addon-page, \.addon-slot, \.addon-card \{[^}]*container-type: inline-size", css)
    assert re.search(r"\.addon-slot \+ \.addon-slot \{[^}]*margin-top: var\(--card-gap\)", css)
    assert re.search(r"\.callout-warn \{[^}]*var\(--warn-bg\)[^}]*var\(--warn-mark\)", css)
    assert re.search(r"\.card-err \{[^}]*var\(--err-mark\)", css)
    assert re.search(r"\.addon-card \.table-wrap \{[^}]*border: 0", css)
    for width, size in (("399px", "s"), ("559px", "m"), ("799px", "l")):
        assert re.search(rf"@container \(max-width: {width}\) \{{[^@]*\.wtable\.cols-{size}", css), width
    assert re.search(r"\.wtable\.cols-xl[^{]*\.cell-key \{[^}]*position: sticky", css)
    assert re.search(r"\.filter-chip\.on \{[^}]*var\(--surface2\)[^}]*inset 0 -2px 0 var\(--text\)", css)


def _css():
    from pathlib import Path
    return (Path(__file__).parents[1] / "src/orch/dashboard/static/app.css").read_text(encoding="utf-8")


def test_a_moved_key_column_is_still_the_stacked_row_title():
    html = render(Table(("State", "Repo", "Run"), (("open", "a/b", "r1"),), key=2))
    assert '<th scope="col" role="columnheader" class="cell-key">Run</th>' in html
    assert '<td role="cell" class="cell-key" data-label="Run">r1</td>' in html
    for size in "sml":
        body = re.search(rf"\.wtable\.cols-{size} td\.cell-key \{{([^}}]*)\}}", _css()).group(1)
        assert "order: -1" in body, size


def test_sticky_column_is_the_key_column_header_and_cells():
    assert re.search(r"\.wtable\.cols-xl \.cell-key \{[^}]*position: sticky", _css())
    assert "th:first-child" not in re.search(r"([^}]*)\{[^}]*position: sticky", _css()).group(1)


def test_the_three_stacked_row_blocks_stay_identical():
    """Container query conditions cannot share one rule body (style queries are not baseline yet), so the three
    widths repeat it; this keeps them from drifting apart."""
    css = _css()
    bodies = [re.search(rf"@container \(max-width: {w}px\) \{{\n((?:  \.wtable\.cols-{s}.*\n)+)\}}", css).group(1)
              .replace(f"cols-{s}", "cols-X") for w, s in (("399", "s"), ("559", "m"), ("799", "l"))]
    assert bodies[0] == bodies[1] == bodies[2]


def test_legacy_stacking_rules_leave_widget_tables_alone():
    css = _css()
    phone = "".join(re.findall(r"@media \(max-width: 720px\) \{(.*?)\n\}", css, re.S))
    assert not re.search(r"(^|[\s,])\.table(\s|,|\{)", phone)  # every legacy rule is .table:not(.wtable)
    assert re.search(r"\.table:not\(\.wtable\) tr \{[^}]*display: flex", phone)
    assert not re.search(r"aside \.table(\s|,|\{)", css)


def test_paragraph_widgets_have_no_default_margins_in_gap_containers():
    assert re.search(r"\.addon-slot > p, \.addon-page > p, \.addon-card > p \{ margin: 0; \}", _css())


def test_search_and_two_chips_rows_get_visible_labels():
    """F: the Search widget has a visible label; two labelled Chips rows in one card show their labels as text."""
    from orch.addons.widgets import Card, Chips, Link, Search
    from orch.dashboard.views import TEMPLATES
    w = TEMPLATES.env.get_template("_widgets.html").module
    g = type("G", (), {"addon": "demo", "key_prefix": "L", "uploads": {}, "confirms": {}, "title": "Demo"})()
    html = str(w.widget(Search("q", "", "Title or text"), g))
    assert '<label class="filter-field filter-search"><span class="filter-label">Search</span><input type="search" name="q"' in html
    two = str(w.widget(Card("Filters", (Chips((Link("Mine", "/addons/demo/?s=m", current=True),), label="Show"),
                                        Chips((Link("acme/a", "/addons/demo/?r=a"),), label="Repo"))), g))
    assert two.count('<div class="chips-row"><span class="filter-label" aria-hidden="true">') == 2
    assert '>Show</span><ul class="widget-chips" aria-label="Show">' in two
    one = str(w.widget(Card("Filters", (Chips((Link("Mine", "/addons/demo/?s=m"),), label="Show"),)), g))
    assert "chips-row" not in one and 'aria-label="Show"' in one


def test_markdown_renders_formatting_but_never_raw_html():
    from orch.addons.widgets import Markdown
    html = render(Markdown('# Title\n\n**bold** and `code`\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n'
                           '[bad](javascript:alert(1)) [art](artifacts/x.png)'))
    assert "<h1>Title</h1>" in html and "<strong>bold</strong>" in html and "<code>code</code>" in html
    assert "<script" not in html and "<img" not in html and "onerror" not in html.replace("&lt;img src=x onerror", "")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert 'href="javascript' not in html
    assert 'href="artifacts' not in html and "/a/" not in html  # R24: a relative path is inert, never an artifact


def test_markdown_widget_never_links_a_ticket_artifact():
    """Page text is bound by no gate: no form of artifact link becomes a live href or an image, on any page."""
    from orch.addons.widgets import Markdown
    text = ("[a](/a/DEMO-0001/shot.png) [b](artifacts/DEMO-0001/shot.png) [c](../artifacts/DEMO-0001/shot.png) "
            "[d](artifact:shot.png) [e](/a/DEMO-0001/shot.png?v=0123456789abcdef) [f](/%61/DEMO-0001/shot.png)\n\n"
            "![g](/a/DEMO-0001/shot.png) ![h](artifact:shot.png) ![i](https://example.com/x.png)\n\n"
            "[ok](https://example.com/doc) [home](/tickets) [top](#top)")
    html = render(Markdown(text))
    assert "/a/" not in html and "artifacts/" not in html and "artifact:" not in html and "<img" not in html
    assert 'href="https://example.com/doc"' in html and 'href="/tickets"' in html and 'href="#top"' in html
    assert 'href="https://example.com/x.png"' in html  # a web image is a link, never loaded


def test_markdown_widget_is_checked():
    from addon_fixtures import GOOD
    from orch.addons.manifest import parse_manifest
    from orch.addons.widgets import MAX_MARKDOWN_LEN, Markdown, widget_problems
    m = parse_manifest(GOOD)
    assert widget_problems(Markdown("# ok"), slot="page.hello-status", manifest=m) == []
    assert widget_problems(Markdown("x" * (MAX_MARKDOWN_LEN + 1)), slot="page.hello-status", manifest=m)
    assert widget_problems(Markdown(3), slot="page.hello-status", manifest=m)


PAGES = frozenset({"Home", "decisions/DEMO-0009", "decisions/sub/deep", "guide/Setup Notes"})


@pytest.mark.parametrize("href,want", [
    ("../Home.md", "/addons/wiki/?page=Home"),
    ("../Home", "/addons/wiki/?page=Home"),
    ("./DEMO-0009.md", "/addons/wiki/?page=decisions%2FDEMO-0009"),
    ("DEMO-0009.md#why", "/addons/wiki/?page=decisions%2FDEMO-0009#why"),
    ("sub/deep.md", "/addons/wiki/?page=decisions%2Fsub%2Fdeep"),
    ("sub/../../Home.md", "/addons/wiki/?page=Home"),
    ("%2E%2E/Home.md", "/addons/wiki/?page=Home"),  # decoded once, then resolved: still inside
    ("../guide/Setup%20Notes.md", "/addons/wiki/?page=guide%2FSetup%20Notes"),
])
def test_page_link_reaches_listed_pages_inside_the_folder(href, want):
    from orch.dashboard.markdown import PageScope, page_link
    assert page_link(href, PageScope("wiki", "decisions/DEMO-0009", PAGES)) == want


@pytest.mark.parametrize("href", [
    "../../Home.md", "../../../etc/passwd", "%2E%2E/%2E%2E/Home.md", "..%2F..%2FHome.md", "%252E%252E/Home.md",
    "/Home.md", "//evil.example/Home.md", "/\\evil.example/Home.md", "///Home.md", "\\Home.md", "..\\Home.md",
    "Missing.md", "?page=Home", "../Home.md?x=1", "https://example.com/Home.md", "javascript:alert(1)",
    "../Home .md", "", "#top", "../../artifacts/DEMO-0001/x.png", "DEMO-0009%00.md",
])
def test_page_link_refuses_escapes_and_non_pages(href):
    from orch.dashboard.markdown import PageScope, page_link
    assert page_link(href, PageScope("wiki", "decisions/DEMO-0009", PAGES)) is None


def test_markdown_widget_links_pages_and_keeps_artifacts_inert():
    from orch.addons.widgets import Markdown
    g = SimpleNamespace(addon="wiki", widgets=(Markdown("[h](../Home.md) [x](../../x.md) [a](/a/DEMO-0001/s.png) "
                                                        "[b](artifacts/DEMO-0001/s.png)", here="decisions/DEMO-0009",
                                                        pages=tuple(sorted(PAGES))),), confirms={}, uploads={},
                        key_prefix="DEMO")
    from orch.dashboard.views import TEMPLATES
    html = TEMPLATES.env.from_string('{% import "_widgets.html" as w %}{{ w.widgets(g.widgets, g) }}').render(g=g)
    assert 'href="/addons/wiki/?page=Home"' in html
    assert html.count("href=") == 1 and "/a/" not in html and "artifacts/" not in html
