import pytest

from addon_fixtures import GOOD
from orch.addons.manifest import parse_manifest
from orch.addons.widgets import (KV, Action, Badge, Callout, Card, Chips, Copy, Link, Table, Text, Tile, safe_url,
                                 widget_problems)

M = parse_manifest({**GOOD, "actions": [{"id": "rerun", "label": "Rerun failed"}]})


def problems(w, slot="page.hello-status"):
    return widget_problems(w, slot=slot, manifest=M)


def test_valid_widgets_pass():
    card = Card("Status", (KV((("Branch", Badge("info", "main")), ("Docs", Link("Open", "https://example.com")))),
                           Table(("Repo", "State"), (("a/b", Badge("ok", "passed")), ("c/d", None))),
                           Callout("warn", "Heads up", "text"), Copy("Copy command", "gh auth login"),
                           Action("rerun", "Rerun failed", "a/b#1"), Text("plain")))
    assert problems(card) == []
    assert problems(Tile("Checks failing", 2, "err", href="/addons/hello-status/"), slot="today.summary") == []


@pytest.mark.parametrize("w, needle", [
    (Badge("you", "x"), "role"),
    (Link("x", "javascript:alert(1)"), "url"),
    (Link("x", "//evil.example"), "url"),
    (Table(("A", "B"), (("only one",),)), "columns"),
    (Action("deploy", "Deploy"), "not declared"),
    (Tile("x", 1), "today.summary"),
    ("<b>raw html</b>", "not a widget"),
    (Card("x", (Card("y", (Card("z", (Card("too deep"),)),)),)), "nested"),
])
def test_bad_widgets_are_reported(w, needle):
    assert any(needle in p for p in problems(w)), problems(w)


def test_summary_slot_takes_only_tiles():
    assert any("only Tile" in p for p in problems(Callout("info", "x"), slot="today.summary"))


def test_safe_url():
    assert safe_url("https://github.com/a/b/pull/1") and safe_url("/t/L-0001")
    for bad in ("javascript:x", "data:text/html,x", "//evil", "ftp://x", " https://x", "\\\\evil"):
        assert not safe_url(bad)


@pytest.mark.parametrize("w, needle", [
    (Text(12345), "must be a string"),
    (Badge("info", 123), "must be a string"),
    (Badge(123, "x"), "role"),
    (Copy(None, "ok"), "must be a string"),
    (Action(123, "Label"), "must be a string"),
    (Callout("info", "t", 7.0), "must be a string"),
    (Card(99), "must be a string"),
])
def test_field_types_are_checked(w, needle):
    assert any(needle in p for p in problems(w)), problems(w)


def test_tile_value_type_is_checked():
    assert any("value" in p for p in problems(Tile("x", object()), slot="today.summary"))
    assert problems(Tile("x", None), slot="today.summary") == []
    assert problems(Tile("x", "ok"), slot="today.summary") == []
    assert problems(Tile("x", 1.5), slot="today.summary") == []


@pytest.mark.parametrize("w, needle", [
    (Text("x" * 20001), "20000"),
    (Copy("y" * 201, "ok"), "200"),
    (Action("rerun", "Rerun failed", "z" * 501), "500"),
])
def test_length_caps_are_enforced(w, needle):
    assert any(needle in p for p in problems(w)), problems(w)


def test_too_many_rows_is_reported():
    rows = tuple((f"L{i}", "v") for i in range(501))
    assert any("rows" in p for p in problems(KV(rows)))
    assert any("rows" in p for p in problems(Table(("A",), tuple((str(i),) for i in range(501)))))


def test_card_body_is_capped():
    assert problems(Card("x", tuple(Text("a") for _ in range(500)))) == []
    assert any("body" in p for p in problems(Card("x", tuple(Text("a") for _ in range(501)))))


def test_tile_value_length_is_capped():
    assert problems(Tile("x", "y" * 200), slot="today.summary") == []
    assert any("value" in p for p in problems(Tile("x", "y" * 201), slot="today.summary"))


@pytest.mark.parametrize("w, needle", [
    (Link("x", "https://example.com/" + "a" * 2000), "url"),
    (Card("x", (), href="/t/" + "a" * 2000), "href"),
])
def test_url_length_is_capped(w, needle):
    assert any(needle in p for p in problems(w)), problems(w)


def test_table_columns_count_is_capped():
    cols = tuple(f"C{i}" for i in range(50))
    assert problems(Table(cols, ())) == []
    cols51 = tuple(f"C{i}" for i in range(51))
    assert any("columns" in p for p in problems(Table(cols51, ())))


@pytest.mark.parametrize("w, needle", [
    (Card("x", None), "body"),
    (KV(None), "rows"),
    (Table(("A", "B"), None), "rows"),
    (Table(None, (("a", "b"),)), "columns"),
    (Table(("A",), ("not a tuple",)), "cell per column"),
])
def test_malformed_containers_never_raise(w, needle):
    result = problems(w)
    assert any(needle in p for p in result), result


@pytest.mark.parametrize("name", ["msg", "err", "token"])
def test_search_cannot_use_core_query_keys(name):
    from orch.addons.widgets import Search
    assert any("core's own query key" in p for p in problems(Search(name)))


def test_layouts_and_chips_pass():
    grid = Card("Repos", (Card("a", (Chips((Text("harness"), Badge("ok", "GitHub"), Link("main", "/x")), label="a"),
                                     KV((("Open", 3), ("Failing", Badge("err", "1"))), layout="stats"))),), layout="grid")
    assert problems(grid) == []
    assert problems(Chips((Link("All", "/addons/x/", current=True), Link("Mine", "/addons/x/?s=mine")))) == []


@pytest.mark.parametrize("w, needle", [
    (Card("x", layout="masonry"), "layout"),
    (KV((), layout="grid"), "layout"),
    (Chips((Table(("A",), ()),)), "not allowed in Chips"),
    (Chips((Action("rerun", "Rerun"),)), "not allowed in Chips"),
    (Chips(None), "must be a tuple"),
    (Chips((), label=5), "must be a string"),
    (Link("x", "/y", current="yes"), "current"),
    (Chips((Link("x", "javascript:1"),)), "url"),
])
def test_bad_layouts_and_chips_are_reported(w, needle):
    assert any(needle in p for p in problems(w)), problems(w)


def _render(*widgets):
    from pathlib import Path
    from types import SimpleNamespace

    import jinja2
    import orch.dashboard
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(orch.dashboard.__file__).with_name("templates"))),
                             autoescape=True)
    g = SimpleNamespace(addon="hello-status", widgets=widgets, confirms={})
    return env.from_string('{% import "_widgets.html" as w %}{% for x in g.widgets %}{{ w.widget(x, g) }}{% endfor %}').render(g=g)


def test_grid_chips_and_stats_render():
    pytest.importorskip("jinja2")
    html = _render(Card("Repos", (Card("a", (KV((("Open", 3),), layout="stats"),)),), layout="grid"),
                   Chips((Link("All <b>", "/addons/x/", current=True), Link("Mine", "/addons/x/?s=mine"), Badge("warn", "2 files"),
                          Text("harness")), label="Filter"))
    assert '<section class="card addon-card card-grid"><h2>Repos</h2><div class="widget-grid"><section class="card addon-card"><h3>a</h3>' in html
    assert '<dl class="kv-stats"><div><dd>3</dd><dt>Open</dt></div></dl>' in html
    assert '<ul class="widget-chips" aria-label="Filter">' in html
    assert '<a class="filter-chip on" href="/addons/x/" aria-current="true">All &lt;b&gt;</a>' in html
    assert '<a class="filter-chip" href="/addons/x/?s=mine">Mine</a>' in html
    assert '<span class="chip chip-warn"><svg class="i" aria-hidden="true"><use href="#i-warn"/></svg> 2 files</span>' in html
    assert '<span class="chip-text">harness</span>' in html


def test_long_text_cells_wrap_but_short_ones_stay_on_one_line():
    pytest.importorskip("jinja2")
    title = "Transform drops timezone-naive reads instead of assuming UTC"
    html = _render(Table(("Key", "Title", "Sprint"), (("GH-11", Text(title), "Sprint 42"), ("GH-12", title, None))))
    assert html.count('class="cell-text"') == 2
    # design-system spec §3.1 Table: ARIA roles on every cell, the first column is the row's key
    assert '<td role="cell" data-label="Sprint">Sprint 42</td>' in html
    assert '<td role="cell" class="cell-key" data-label="Key">GH-11</td>' in html
