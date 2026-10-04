"""Core widget types, set A: bars, runs, spark, series, bullet, scores, gantt, tests, gates, diffstat."""
import copy
import json
import re

import pytest

from orch.core.model import Ticket
from orch.widgets import Ctx, check_ticket, registry, render_document, render_html, render_text

NAMES = ["bars", "runs", "spark", "series", "bullet", "scores", "gantt", "tests", "gates", "diffstat"]
EVIL = "<script>alert(1)</script>"


def block(obj):
    t = Ticket(meta={"id": "L-0001", "title": "x"}, sections={"Context": f"```orch\n{json.dumps(obj)}\n```"})
    [b] = check_ticket(t)
    return b


def html_of(obj) -> str:
    b = block(obj)
    assert b.problems == [], b.problems
    return str(render_html(b, Ctx()))


def text_of(obj) -> str:
    return render_text(block(obj), Ctx())


def widths(html: str, cls: str) -> list[float]:
    return [float(w) for w in re.findall(rf'<i[^>]*style="width:([\d.]+)%"', html)]


@pytest.mark.parametrize("name", NAMES)
def test_example_valid_and_rendered(name):
    mod = registry.core_types()[name]
    html = html_of(mod.EXAMPLE)
    assert f'w-t-{name}' in html and "<script" not in html
    assert render_text(block(mod.EXAMPLE), Ctx()).startswith("[")
    assert "<script" not in render_document(block(mod.EXAMPLE), Ctx())


BAD = {
    "bars": [{"data": {}}, {"data": {"a": -1}}, {"data": {"a": "x"}}, {"data": [["a"]]}, {"data": {"a": 1}, "x": 1}],
    "runs": [{"values": []}, {"values": ["a"]}, {"values": [1], "marks": {"0": "n"}}, {"values": [1], "marks": {"1": ""}}],
    "spark": [{"values": [1], "text": "a {spark} b"}, {"values": [1, 2], "text": "no placeholder"},
              {"values": [1, 2], "text": "{spark} twice {spark}"}],
    "series": [{"points": [[1, 2]]}, {"points": [[1, 2], [2]]}, {"points": [[1, 2], [2, 3]], "markers": [{"x": 1}]}],
    "bullet": [{"value": 1, "target": 2}, {"value": 1, "target": 2, "bands": [1, 2]}, {"value": "a", "target": 2, "bands": [1, 2, 3]}],
    "scores": [{"items": {}}, {"items": {"a": 101}}, {"items": {"a": -1}}],
    "gantt": [{"lanes": {}}, {"lanes": {"a": []}}, {"lanes": {"a": [[1]]}}],
    "tests": [{"added": 1, "fixed": 1}, {"added": -1, "fixed": 0, "broke": 0}, {"added": 1.5, "fixed": 0, "broke": 0},
              {"added": 0, "fixed": 0, "broke": 0, "names": {"nope": []}}],
    "gates": [{"items": [{"name": "a", "status": "ok"}]}, {"items": [{"name": "a", "status": "pass", "seconds": -1}]}, {"items": []}],
    "diffstat": [{"files": [{"path": "a", "add": 1}]}, {"files": [{"path": "a", "add": -1, "del": 0}]}, {"files": []}],
}


@pytest.mark.parametrize("name", NAMES)
def test_schema_rejects_bad_data(name):
    for bad in BAD[name]:
        assert block({"type": name, **bad}).problems, bad
    extra = {**copy.deepcopy(registry.core_types()[name].EXAMPLE), "surplus": 1}
    assert block(extra).problems


EVIL_BLOCKS = {
    "bars": {"data": {EVIL: 3}, "unit": "<u>", "highlight": EVIL},
    "runs": {"values": [1, 2, 3], "marks": {"2": EVIL}},
    "spark": {"values": [1, 2], "text": f"{EVIL} {{spark}} {EVIL}"},
    "series": {"points": [[1, 1], [2, 2]], "markers": [{"x": 1, "label": EVIL}]},
    "bullet": {"value": 1, "target": 2, "bands": [1, 2, 3], "unit": "<u>"},
    "scores": {"items": {EVIL: 50}},
    "gantt": {"lanes": {EVIL: [[0, 1]]}, "unit": "<u>"},
    "tests": {"added": 1, "fixed": 1, "broke": 1, "names": {"broke": [EVIL]}},
    "gates": {"items": [{"name": EVIL, "status": "pass", "seconds": 3}]},
    "diffstat": {"files": [{"path": EVIL, "add": 1, "del": 1}]},
}


@pytest.mark.parametrize("name", NAMES)
def test_html_escapes_injected_markup(name):
    obj = {"type": name, "title": EVIL, **EVIL_BLOCKS[name]}
    html = html_of(obj)
    assert "<script" not in html and "<u>" not in html and "&lt;script&gt;" in html


@pytest.mark.parametrize("name", NAMES)
def test_text_alternative_carries_every_number(name):
    for token in NUMBERS[name]:
        assert str(token) in text_of({"type": name, **EXAMPLES[name]}), (name, token)


EXAMPLES = {
    "bars": {"data": {"main": 412, "branch": 286.5}, "unit": "kB"},
    "runs": {"values": [412, 405, 398.5, 880], "marks": {"4": "cold"}, "unit": "ms"},
    "spark": {"values": [14, 13, 11.5, 6], "text": "CI {spark} now"},
    "series": {"points": [[1, 120], [2, 118.5], [7, 131]], "markers": [{"x": 4, "label": "deploy"}]},
    "bullet": {"value": 286, "target": 300, "bands": [300, 350, 450], "lower_is_better": True, "unit": "kB"},
    "scores": {"items": {"perf": 91, "a11y": 100, "seo": 72, "pwa": 38}},
    "gantt": {"lanes": {"build": [[0, 12], [30, 41]], "test": [[12, 30]]}, "unit": "min"},
    "tests": {"added": 6, "fixed": 2, "broke": 1, "flaky": 3, "total": 1284},
    "gates": {"items": [{"name": "build", "status": "pass", "seconds": 94}, {"name": "t", "status": "fail", "seconds": 212}]},
    "diffstat": {"files": [{"path": "a.py", "add": 42, "del": 7}, {"path": "b.py", "add": 120, "del": 0}]},
}
NUMBERS = {
    "bars": [412, 286.5], "runs": [412, 405, 398.5, 880, "median"], "spark": [14, 13, 11.5, 6],
    "series": [1, 2, 7, 120, 118.5, 131, 4], "bullet": [286, 300, 350, 450], "scores": [91, 100, 72, 38],
    "gantt": [0, 12, 30, 41], "tests": [6, 2, 1, 3, 1284], "gates": ["1m 34s", 94, "3m 32s", 212],
    "diffstat": [42, 7, 120, 0, 162, 7],
}


def test_bars_widths_are_proportional():
    w = widths(html_of({"type": "bars", "data": [["a", 50], ["b", 200], ["c", 100], ["z", 0]]}), "")
    assert w == [25, 100, 50, 0]


def test_bars_highlight_has_a_word_not_just_hue():
    html = html_of({"type": "bars", "data": {"a": 1, "b": 2}, "highlight": "b"})
    assert html.count("highlighted: ") == 1 and "w-hot" in html


def test_bars_accept_pairs_and_dict_the_same():
    a = html_of({"type": "bars", "data": {"x": 1, "y": 3}})
    b = html_of({"type": "bars", "data": [["x", 1], ["y", 3]]})
    assert a.replace(" ", "") == b.replace(" ", "")


def test_runs_columns_scale_with_values_and_marks_get_badges():
    html = html_of({"type": "runs", "values": [100, 50, 200], "marks": {"3": "slow"}})
    h = [float(x) for x in re.findall(r'class="w-col(?: w-marked)?"[^>]* height="([\d.]+)"', html)]
    assert h[0] / h[2] == pytest.approx(0.5, rel=1e-3) and h[1] / h[2] == pytest.approx(0.25, rel=1e-3)
    assert "▼3" in html and "Run 3" in html and "slow" in html and "median 100" in html


def test_runs_ignores_marks_beyond_the_values():
    html = html_of({"type": "runs", "values": [1, 2], "marks": {"9": "gone"}})
    assert "gone" not in html and "▼" not in html


def test_spark_puts_the_line_inside_the_sentence():
    html = html_of({"type": "spark", "values": [1, 3, 2], "text": "before {spark} after"})
    assert re.search(r"before <svg.*</svg> after</p>", html) and 'aria-label="trend of 3 values' in html
    assert "min 1, max 3" in html
    t = text_of({"type": "spark", "values": [1, 3, 2], "text": "before {spark} after"})
    assert "before [1, 3, 2] after" in t


def test_spark_flat_series_does_not_divide_by_zero():
    assert "<polyline" in html_of({"type": "spark", "values": [5, 5, 5], "text": "flat {spark}"})


def test_series_points_markers_and_labelled_values():
    html = html_of({"type": "series", "unit": "ms", "points": [[3, 30], [1, 10], [2, 20]],
                    "markers": [{"x": 2, "label": "deploy"}]})
    assert "10 ms" in html and "30 ms" in html and "deploy" in html and 'class="w-rule"' in html
    pts = re.search(r'class="w-line" points="([^"]+)"', html).group(1).split()
    xs = [float(q.split(",")[0]) for q in pts]
    assert xs == sorted(xs) and (xs[1] - xs[0]) == pytest.approx(xs[2] - xs[1], rel=1e-3)  # drawn in x order, even steps


def test_series_flat_and_single_x_do_not_crash():
    html_of({"type": "series", "points": [[1, 5], [2, 5]]})
    html_of({"type": "series", "points": [[1, 5], [1, 6]]})


def test_bullet_verdicts_both_directions():
    low = {"type": "bullet", "target": 300, "bands": [300, 350, 450], "lower_is_better": True}
    assert "Good" in html_of({**low, "value": 286}) and "OK" in html_of({**low, "value": 320})
    assert "Bad" in html_of({**low, "value": 400})
    high = {"type": "bullet", "target": 90, "bands": [90, 70, 0]}
    assert "Good" in html_of({**high, "value": 95}) and "Bad" in html_of({**high, "value": 40})
    assert "lower is better" in html_of({**low, "value": 286}) and "lower is better" not in html_of({**high, "value": 95})


def test_bullet_geometry():
    html = html_of({"type": "bullet", "value": 50, "target": 75, "bands": [100, 80, 0]})
    # the scale is 0..100 plus 15% room for the open-ended good zone
    assert 'class="w-bl-val" style="width:43.478%"' in html and 'class="w-bl-tgt" style="left:65.217%"' in html


def test_scores_use_lighthouse_bands():
    t = text_of({"type": "scores", "items": {"a": 0, "b": 49, "c": 50, "d": 89, "e": 90, "f": 100}})
    got = dict(re.findall(r"^(\w): [\d.]+/100 \((.+)\)$", t, re.M))
    assert got == {"a": "Poor", "b": "Poor", "c": "Needs work", "d": "Needs work", "e": "Good", "f": "Good"}
    html = html_of({"type": "scores", "items": {"a": 91, "b": 50, "c": 10}})
    assert "w-r-ok" in html and "w-r-warn" in html and "w-r-err" in html
    assert 'stroke-dasharray="91 100"' in html and "Good" in html and "Poor" in html


def test_gantt_positions_follow_the_shared_scale():
    html = html_of({"type": "gantt", "lanes": {"a": [[0, 50]], "b": [[50, 100]]}})
    bars = re.findall(r'class="w-gb" style="left:([\d.]+)%;width:([\d.]+)%', html)
    assert bars == [("0", "50"), ("50", "50")]
    ticks = re.findall(r'<span style="left:([\d.]+)%">([\d.]+)</span>', html)
    assert ticks and all(float(p) == pytest.approx(float(v)) for p, v in ticks)  # a 0-100 scale: tick value = percent


def test_gantt_packs_overlaps_onto_rows_and_keeps_zero_length_visible():
    html = html_of({"type": "gantt", "lanes": {"a": [[0, 10], [5, 15], [20, 20]]}})
    assert "--rows:2" in html and "top:calc(1 * var(--w-gh))" in html
    assert 'width:0.8%' in html


def test_gantt_bars_carry_their_values_for_the_stylesheet_to_show_when_there_is_room():
    html = html_of({"type": "gantt", "unit": "min", "lanes": {"a": [[0, 12.5]]}})
    assert '<span class="w-gv" aria-hidden="true">0–12.5 min</span>' in html


def test_runs_columns_stay_slim_with_few_values():
    html = html_of({"type": "runs", "values": [1, 2, 3]})
    assert all(float(w) <= 28 for w in re.findall(r'class="w-col"[^>]* width="([\d.]+)"', html))


def test_series_labels_never_touch_each_other_and_high_low_win():
    html = html_of({"type": "series", "unit": "ms", "points": [[1, 100], [2, 300], [3, 10], [4, 290], [5, 50], [6, 120]]})
    vals = re.findall(r'class="w-val"[^>]*>([^<]+)<', html)
    assert "300 ms" in vals and "10 ms" in vals and "100 ms" not in vals  # first dropped: it would touch the high


def test_tests_show_delta_and_names():
    html = html_of({"type": "tests", "added": 3, "fixed": 0, "broke": 2, "names": {"broke": ["t::a", "t::b"]}})
    assert "w-r-err" in html and "w-zero" in html and "t::a" in html and "broke" in html
    assert render_text(block({"type": "tests", "added": 3, "fixed": 0, "broke": 2}), Ctx()).splitlines()[1] == \
        "2 broke, 0 fixed, 3 added"


def test_gates_status_words_and_bar_ratio():
    html = html_of({"type": "gates", "items": [
        {"name": "a", "status": "pass", "seconds": 50}, {"name": "b", "status": "fail", "seconds": 100},
        {"name": "c", "status": "skip"}, {"name": "d", "status": "running"}]})
    assert widths(html, "") == [50, 100]
    for word in ("Pass", "Fail", "Skipped", "Running"):
        assert word in html


def test_diffstat_bars_and_totals():
    html = html_of({"type": "diffstat", "files": [{"path": "a", "add": 30, "del": 10}, {"path": "b", "add": 10, "del": 30},
                                                  {"path": "c", "add": 0, "del": 0}]})
    w = widths(html, "")
    assert w[0] == pytest.approx(75) and w[1] == pytest.approx(25) and w[2] == pytest.approx(25) and w[3] == pytest.approx(75)
    assert w[4:] == [0, 0]
    assert "3 files changed" in html and "+40" in html and "−40" in html
    assert "1 file changed" in html_of({"type": "diffstat", "files": [{"path": "a", "add": 0, "del": 0}]})


def test_widget_stylesheet_has_only_tokens_for_colour():
    css = (registry.__file__ and __import__("orch.widgets.render", fromlist=["x"]).CSS_DIR / "bars.css").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", css)
