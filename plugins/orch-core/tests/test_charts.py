"""The Chart widget (API 2.6), the chart macro and its table fallback, and the vendored Chart.js."""
import hashlib
import json
import re

import pytest

from addon_fixtures import GOOD
from orch.addons.manifest import parse_manifest
from orch.addons.widgets import Card, Chart, ChartSeries, widget_problems

M = parse_manifest(GOOD)


def problems(w, slot="page.hello-status"):
    return widget_problems(w, slot=slot, manifest=M)


def ok(**over):
    base = dict(title="Runs per day", labels=("Mon", "Tue"), series=(ChartSeries("Passed", (3, 4.5)),))
    return Chart(**{**base, **over})


def test_a_valid_chart_passes_alone_and_in_a_card():
    assert problems(ok()) == []
    assert problems(Card("Runs", (ok(stacked=True, series=(ChartSeries("A", (1, 2), "series-2", "x"),
                                                               ChartSeries("B", (1, 2), "ok-mark", "x"))),))) == []
    assert problems(ok(x="linear", labels=(0, 2.5), style="line", unit="h")) == []
    assert problems(ok(x="time", labels=(1_790_000_000, 1_790_003_600), style="line")) == []
    assert problems(ok(title="")) == []


@pytest.mark.parametrize("w, needle", [
    (ok(title=" "), "empty"),
    (ok(style="pie"), "style"),
    (ok(x="log"), "x"),
    (ok(stacked="yes"), "stacked"),
    (ok(style="line", horizontal=True), "horizontal"),
    (ok(labels=()), "labels"),
    (ok(labels=("a", 2)), "labels[1]"),
    (ok(x="linear"), "number"),
    (ok(x="time"), "number"),
    (ok(x="time", labels=(1, 2), horizontal=True), "horizontal"),
    (ok(series=()), "series"),
    (ok(series=tuple(ChartSeries(f"s{i}", (1, 2)) for i in range(9))), "series"),
    (ok(series=(ChartSeries("A", (1,)),)), "one number per label"),
    (ok(series=(ChartSeries("A", (1, "2")),)), "numbers only"),
    (ok(series=(ChartSeries("A", (1, None)),)), "numbers only"),
    (ok(series=(ChartSeries("A", (1, float("nan"))),)), "numbers only"),
    (ok(series=(ChartSeries("A", (True, 2)),)), "numbers only"),
    (ok(series=(ChartSeries("A", (1, 2), "url(x)"),)), "token"),
    (ok(series=(ChartSeries("A", (1, 2), "", "A B"),)), "stack"),
    (ok(series=("A",)), "ChartSeries"),
    (ok(unit="x" * 21), "unit"),
    (ok(labels=tuple(str(i) for i in range(401)), series=(ChartSeries("A", (1,) * 401),)), "400"),
])
def test_bad_charts_are_reported(w, needle):
    assert any(needle in p for p in problems(w)), problems(w)


def test_a_chart_page_renders_the_figure_the_table_and_the_legend(dash):
    from orch.dashboard.views import TEMPLATES
    html = TEMPLATES.env.from_string(
        '{% import "_widgets.html" as w %}{{ w.widget(x, g) }}').render(
        x=Card("Runs", (ok(series=(ChartSeries("Passed", (3, 4.5)), ChartSeries("Failed", (0, 1)))),)), g=type("G", (), {"addon": "a", "uploads": ()})())
    assert '<figure class="chart" data-chart=' in html
    spec = json.loads(re.search(r"data-chart='([^']*)'", html).group(1))
    assert spec["labels"] == ["Mon", "Tue"] and [s["token"] for s in spec["series"]] == ["series-1", "series-2"]
    assert '<details class="chart-data">' in html and "<table" in html
    assert "<td class=\"num\">4.5</td>" in html and "chart-legend" in html
    assert "data-lib" not in html  # charts.js knows the one vendored path itself


def test_a_single_series_has_no_legend():
    from orch.dashboard.views import TEMPLATES
    html = TEMPLATES.env.from_string('{% from "_charts.html" import chart %}{{ chart(spec, "T") }}').render(
        spec={"kind": "bar", "labels": ["a"], "series": [{"name": "n", "values": [1], "token": "series-1"}],
              "stacked": False, "unit": "", "x": "category", "horizontal": False})
    assert "chart-legend" not in html and "<table" in html


def test_a_hostile_label_cannot_leave_the_attribute():
    from orch.dashboard.views import TEMPLATES
    nasty = "'><script>alert(1)</script>"
    html = TEMPLATES.env.from_string('{% from "_charts.html" import chart %}{{ chart(spec, "T") }}').render(
        spec={"kind": "bar", "labels": [nasty], "series": [{"name": nasty, "values": [1], "token": "series-1"}],
              "stacked": False, "unit": "", "x": "category", "horizontal": False})
    assert "<script>" not in html


def test_the_vendored_chart_library_is_served_and_is_the_one_in_the_readme(dash):
    r = dash.get("/static/vendor/chartjs/chart.umd.min.js")
    assert r.status_code == 200 and b"Chart.js v4.5.0" in r.content[:200]
    readme = dash.get("/static/vendor/chartjs/README.md").text
    assert hashlib.sha256(r.content).hexdigest() in readme
    assert "MIT License" in readme and "cdnjs.cloudflare.com" in readme
    assert dash.get("/static/charts.js").status_code == 200


def test_the_series_tokens_exist_in_both_themes():
    from pathlib import Path
    css = (Path(__file__).parents[1] / "src/orch/dashboard/static/tokens.css").read_text()
    for i in range(1, 9):
        assert css.count(f"--series-{i}:") >= 2


def test_eight_series_never_share_a_colour():
    w = ok(labels=("a",), series=tuple(ChartSeries(f"s{i}", (i,)) for i in range(8)))
    assert problems(w) == []
    assert len({s["token"] for s in w.spec()["series"]}) == 8


def test_an_untitled_chart_has_no_heading_but_an_aria_label_and_a_time_table_shows_clock_times(dash):
    from orch.dashboard.views import TEMPLATES
    w = ok(title="", x="time", style="line", labels=(1_790_000_000, 1_790_003_600))
    html = TEMPLATES.env.from_string('{% import "_widgets.html" as w %}{{ w.widget(x, g) }}').render(
        x=w, g=type("G", (), {"addon": "a", "uploads": ()})())
    assert "chart-title" not in html and 'aria-label="Passed"' in html
    assert re.search(r"<th scope=\"row\">\d\d\.\d\d\. \d\d:\d\d</th>", html)
