"""Core widget types, batch B: health options matrix risk deps flow trail deploy diff compare screens video."""
import hashlib
import json

import pytest

from orch.core.model import Ticket
from orch.widgets import Ctx, check_ticket, registry, render_document, render_html, render_text
from orch.widgets.types import deps, matrix

NAMES = ["health", "options", "matrix", "risk", "deps", "flow", "trail", "deploy", "diff", "compare", "screens", "video"]
XSS = '<script>alert(1)</script>'
F = "```"


def _ticket(body):
    return Ticket(meta={"id": "L-0001", "title": "x"}, sections={"Findings": f"{F}orch\n{json.dumps(body)}\n{F}"})


def block(body, ws=None):
    t = _ticket(body)
    [b] = check_ticket(t, ws=ws)
    return t, b


def problems(body):
    return [p.message for p in block(body)[1].problems]


@pytest.mark.parametrize("name", NAMES)
def test_example_validates_renders_and_has_text(name):
    mod = registry.core_types()[name]
    t, b = block({**mod.EXAMPLE, "title": "T"})
    assert b.problems == []
    html = str(render_html(b, Ctx(ticket=t)))
    assert f"w-t-{name}" in html and "<script" not in html
    assert render_text(b, Ctx(ticket=t)).startswith("[T · core]\n")
    assert "<script" not in render_document(b, Ctx(ticket=t))


BAD = [
    {"type": "health", "health": "fine", "why": "x"}, {"type": "health", "health": "blocked"},
    {"type": "options", "items": []}, {"type": "options", "items": [{"id": "a", "title": "x", "extra": 1}]},
    {"type": "matrix", "criteria": ["a"], "rows": {"o": [6]}}, {"type": "matrix", "criteria": ["a"], "rows": {"o": ["maybe"]}},
    {"type": "risk", "items": {"a": 3}}, {"type": "risk", "items": {}},
    {"type": "deps", "nodes": [{"id": "a"}]}, {"type": "deps", "nodes": [{"id": "a"}], "edges": [["a"]]},
    {"type": "flow", "steps": ["only"]}, {"type": "flow", "steps": ["a", "a"]},
    {"type": "trail", "items": [{"t": "1", "kind": "Bad Kind", "text": "x"}]},
    {"type": "deploy", "env": "p", "sha": "xyz", "healthy": True}, {"type": "deploy", "env": "p", "sha": "abcdef1", "healthy": "yes"},
    {"type": "diff", "file": "f", "lines": "\n" * 200}, {"type": "diff", "file": "f"},
    {"type": "compare", "before": {"path": "artifacts/L-0001/a.png", "sha256": "0" * 64}},
    {"type": "compare", "before": {"path": "/etc/passwd", "sha256": "0" * 64}, "after": {"path": "artifacts/L-0001/a.png", "sha256": "0" * 64}},
    {"type": "screens", "items": [{"path": "artifacts/L-0001/a.png", "sha256": "0" * 64}]},
    {"type": "screens", "columns": 9, "items": [{"label": "x", "path": "artifacts/L-0001/a.png", "sha256": "0" * 64}]},
    {"type": "video", "path": "artifacts/L-0001/a.mp4", "sha256": "short"},
]


@pytest.mark.parametrize("body", BAD, ids=lambda b: f'{b["type"]}-{len(json.dumps(b))}')
def test_bad_data_is_rejected(body):
    assert problems(body)


def test_diff_accepts_exactly_200_lines():
    assert not problems({"type": "diff", "file": "f", "lines": "\n".join(["+x"] * 200)})
    assert problems({"type": "diff", "file": "f", "lines": "\n".join(["+x"] * 201)})


def test_injected_markup_is_escaped_everywhere():
    cases = [
        {"type": "health", "health": "blocked", "why": XSS},
        {"type": "options", "pick": "a", "items": [{"id": "a", "title": XSS, "cost": XSS, "risk": XSS, "notes": XSS}]},
        {"type": "matrix", "criteria": [XSS], "rows": {XSS: [1]}, "pick": XSS},
        {"type": "risk", "items": {XSS: XSS}},
        {"type": "deps", "nodes": [{"id": "a", "label": XSS, "status": XSS}], "edges": []},
        {"type": "flow", "steps": [XSS, "b"], "highlight": XSS, "notes": {XSS: XSS}},
        {"type": "trail", "items": [{"t": XSS, "kind": "log", "text": XSS}]},
        {"type": "deploy", "env": XSS, "sha": "abcdef1", "healthy": False, "at": XSS, "url": XSS},
        {"type": "diff", "file": XSS, "lines": f"+{XSS}\n-{XSS}"},
        {"type": "compare", "before": {"path": "artifacts/L-0001/a.png", "sha256": "0" * 64},
         "after": {"path": "artifacts/L-0001/a.png", "sha256": "0" * 64}, "labels": [XSS, XSS]},
        {"type": "screens", "items": [{"label": XSS, "path": "artifacts/L-0001/a.png", "sha256": "0" * 64}]},
    ]
    for body in cases:
        t, b = block({**body, "title": XSS})
        assert b.problems == [], body["type"]
        for out in (str(render_html(b, Ctx(ticket=t))), render_document(b, Ctx(ticket=t))):
            assert "<script>alert" not in out and "&lt;script&gt;" in out, body["type"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "../evil/x", "vbscript:x"])
def test_deploy_unsafe_url_stays_text(url):
    t, b = block({"type": "deploy", "env": "p", "sha": "abcdef1", "healthy": True, "url": url})
    html = str(render_html(b, Ctx(ticket=t)))
    assert "href=" not in html and "<code>" in html


def test_deploy_links_web_urls_and_shortens_sha():
    t, b = block({"type": "deploy", "env": "p", "sha": "0123456789abcdef", "healthy": True, "url": "https://e.test/h"})
    html = str(render_html(b, Ctx(ticket=t)))
    assert 'href="https://e.test/h"' in html and "0123456" in html and "0123456789" not in html and "Healthy" in html
    assert render_text(b, Ctx(ticket=t)).splitlines()[1] == "p @ 0123456: healthy https://e.test/h"


def test_unhealthy_says_so_in_a_word():
    t, b = block({"type": "deploy", "env": "p", "sha": "abcdef1", "healthy": False})
    assert "Unhealthy" in str(render_html(b, Ctx(ticket=t)))
    assert "unhealthy" in render_text(b, Ctx(ticket=t))


def test_health_states_have_words():
    for state, word in (("on_track", "On track"), ("at_risk", "At risk"), ("blocked", "Blocked")):
        t, b = block({"type": "health", "health": state, "why": "w"})
        assert word in str(render_html(b, Ctx(ticket=t))) and render_text(b, Ctx(ticket=t)).endswith(f"{word}: w")


def test_options_marks_the_pick_with_a_word_and_text_says_so():
    t, b = block(registry.core_types()["options"].EXAMPLE)
    html = str(render_html(b, Ctx(ticket=t)))
    assert html.count("Recommended") == 1 and "w-opt-pick" in html
    assert render_text(b, Ctx(ticket=t)).count("[recommended]") == 1


def test_matrix_totals_are_computed_with_weights_and_ticks():
    d = {"criteria": ["a", "b", "c"], "weights": [1, 2], "rows": {"x": [4, 2, "✓"], "y": [1, 1, "✗"], "short": [3]}}
    assert matrix.totals(d) == {"x": 4 + 4 + 1, "y": 1 + 2 + 0, "short": 3}
    t, b = block({"type": "matrix", **d, "pick": "x"})
    html = str(render_html(b, Ctx(ticket=t)))
    assert "w-mx-pick" in html and ">9<" in html and "Recommended" in html and "–" in html  # ragged row is padded
    assert "x: 4 2 ✓ = 9 [recommended]" in render_text(b, Ctx(ticket=t))


def test_risk_flags_by_word_and_text():
    t, b = block({"type": "risk", "items": {"auth": False, "data": "new column", "money": True}})
    html = str(render_html(b, Ctx(ticket=t)))
    assert html.count("Flagged") == 2 and html.count("Clear") == 1
    assert render_text(b, Ctx(ticket=t)).splitlines()[1:] == ["auth: clear", "data: flagged — new column", "money: flagged"]


def test_deps_layout_columns_by_depth_and_survives_cycles_and_bad_edges():
    d = {"nodes": [{"id": i} for i in "abcd"], "edges": [["a", "b"], ["b", "c"], ["a", "c"], ["x", "a"], ["d", "d"]]}
    col, row, edges = deps.layout(d)
    assert (col["a"], col["b"], col["c"], col["d"]) == (0, 1, 2, 0) and row["d"] == 1 and ("x", "a") not in edges
    cyc = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [["a", "b"], ["b", "a"]]}
    deps.layout(cyc)  # terminates
    t, b = block({"type": "deps", **cyc})
    assert "<svg" in str(render_html(b, Ctx(ticket=t)))


def test_deps_svg_has_arrows_status_words_and_text():
    t, b = block(registry.core_types()["deps"].EXAMPLE)
    html = str(render_html(b, Ctx(ticket=t)))
    assert html.count("<path class=\"w-dg-e\"") == 3 and "marker-end" in html and "blocked" in html and "<title>B-3: UI (blocked)</title>" in html
    text = render_text(b, Ctx(ticket=t))
    assert "B-1 Schema [done]" in text and "B-1 -> B-2" in text


def test_flow_highlight_is_a_word_and_aria_current():
    t, b = block(registry.core_types()["flow"].EXAMPLE)
    html = str(render_html(b, Ctx(ticket=t)))
    assert html.count("You are here") == 1 and html.count("aria-current=step") == 1 and "3-D Secure" in html
    assert "3. Pay [you are here] — the card" in render_text(b, Ctx(ticket=t))


def test_trail_marks_errors_with_icon_and_word():
    t, b = block(registry.core_types()["trail"].EXAMPLE)
    html = str(render_html(b, Ctx(ticket=t)))
    assert "w-r-err" in html and ">✕</span>error" in html
    assert render_text(b, Ctx(ticket=t)).splitlines()[-1] == "12:00:03 error: 409 plan changed"


def test_diff_lines_keep_their_glyphs():
    t, b = block(registry.core_types()["diff"].EXAMPLE)
    html = str(render_html(b, Ctx(ticket=t)))
    for cls, glyph in (("add", "+"), ("del", "-"), ("hunk", "@@"), ("ctx", " def")):
        assert f'class="w-dl w-dl-{cls}">{glyph}' in html
    assert render_text(b, Ctx(ticket=t)).startswith("[core]\n--- src/board.py\n@@")


# -- artifact types ----------------------------------------------------------------------------------------------

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f00050001ff"
                    "89993d1d0000000049454e44ae426082")


@pytest.fixture
def art(ws):
    folder = ws.artifacts_dir / "L-0001"
    folder.mkdir(parents=True)
    (folder / "a.png").write_bytes(PNG)
    (folder / "b.png").write_bytes(PNG + b"\0")
    (folder / "r.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 20)
    return {n: hashlib.sha256((folder / n).read_bytes()).hexdigest() for n in ("a.png", "b.png", "r.mp4")}


def pin(name, sha):
    return {"path": f"artifacts/L-0001/{name}", "sha256": sha}


def test_compare_page_links_and_document_embeds(ws, art):
    t, b = block({"type": "compare", "before": pin("a.png", art["a.png"]), "after": pin("b.png", art["b.png"]),
                  "labels": ["Old", "New"]}, ws=ws)
    assert b.problems == []
    page = str(render_html(b, Ctx(ticket=t, ws=ws)))
    assert f'src="/a/L-0001/a.png?v={art["a.png"]}"' in page and 'alt="Old"' in page and "data:image" not in page
    doc = render_document(b, Ctx(ticket=t, ws=ws))
    assert "src=\"data:image/png;base64," in doc and "/a/L-0001" not in doc and "<script" not in doc
    assert "Old: artifacts/L-0001/a.png" in render_text(b, Ctx(ticket=t, ws=ws))


def test_compare_digest_states(ws, art):
    t, changed = block({"type": "compare", "before": pin("a.png", "0" * 64), "after": pin("b.png", art["b.png"])}, ws=ws)
    assert [(p.code, p.level) for p in changed.problems] == [("widget-digest", "warning")]
    html = str(render_html(changed, Ctx(ticket=t, ws=ws)))
    assert "changed since this widget was written" in html  # the rest draws; the changed file is never shown
    assert html.count("<img") == 1 and f"b.png?v={art['b.png']}" in html and "a.png?v=" not in html
    t, missing = block({"type": "compare", "before": pin("gone.png", art["a.png"]), "after": pin("b.png", art["b.png"])}, ws=ws)
    assert [(p.code, p.level) for p in missing.problems] == [("widget-digest", "error")]
    html = str(render_html(missing, Ctx(ticket=t, ws=ws)))
    assert "is missing" in html and "<img" not in html
    t, outside = block({"type": "compare", "before": {"path": "artifacts/L-0002/a.png", "sha256": art["a.png"]},
                        "after": pin("b.png", art["b.png"])}, ws=ws)
    assert outside.problems[0].level == "error"


def test_compare_without_a_workspace_says_it_cannot_show():
    t, b = block(registry.core_types()["compare"].EXAMPLE)
    html = str(render_html(b, Ctx(ticket=t)))
    assert "cannot be shown here" in html and "<img" not in html


def test_screens_grid_columns_and_labels(ws, art):
    t, b = block({"type": "screens", "columns": 2, "items": [
        {"label": "1100 px light", **pin("a.png", art["a.png"])}, {"label": "390 px dark", **pin("b.png", art["b.png"])}]}, ws=ws)
    html = str(render_html(b, Ctx(ticket=t, ws=ws)))
    assert "--cols:2" in html and html.count("<img") == 2 and "390 px dark" in html
    assert render_text(b, Ctx(ticket=t, ws=ws)).splitlines()[2].startswith("390 px dark: artifacts/L-0001/b.png")
    t, b = block({"type": "screens", "items": [{"label": "x", **pin("a.png", "1" * 64)}]}, ws=ws)
    assert b.problems[0].level == "warning"


def test_screens_refuse_a_non_image(ws, art):
    t, b = block({"type": "screens", "items": [{"label": "v", **pin("r.mp4", art["r.mp4"])}]}, ws=ws)
    assert "<img" not in str(render_html(b, Ctx(ticket=t, ws=ws)))


def test_video_is_a_native_element_and_digest_states(ws, art):
    t, b = block({"type": "video", **pin("r.mp4", art["r.mp4"]), "poster": pin("a.png", art["a.png"])}, ws=ws)
    assert b.problems == []
    html = str(render_html(b, Ctx(ticket=t, ws=ws)))
    assert f'<video class="w-video" controls preload="metadata" src="/a/L-0001/r.mp4?v={art["r.mp4"]}" poster="/a/L-0001/a.png?v={art["a.png"]}">' in html
    doc = render_document(b, Ctx(ticket=t, ws=ws))
    assert 'src="data:video/mp4;base64,' in doc and "<script" not in doc
    assert "Recording: artifacts/L-0001/r.mp4" in render_text(b, Ctx(ticket=t, ws=ws))
    t, changed = block({"type": "video", **pin("r.mp4", "0" * 64)}, ws=ws)
    assert changed.problems[0].level == "warning" and "<video" not in str(render_html(changed, Ctx(ticket=t, ws=ws)))
    t, missing = block({"type": "video", **pin("gone.mp4", "0" * 64)}, ws=ws)
    assert missing.problems[0].level == "error" and "<video" not in str(render_html(missing, Ctx(ticket=t, ws=ws)))


def test_video_too_big_for_a_document_is_said_not_embedded(ws, art, monkeypatch):
    from orch.widgets import artifacts
    monkeypatch.setattr(artifacts, "MAX_DATA_URI", 4)
    t, b = block({"type": "video", **pin("r.mp4", art["r.mp4"])}, ws=ws)
    doc = render_document(b, Ctx(ticket=t, ws=ws))
    assert "<video" not in doc and "too big to embed" in doc
    assert "<video" in str(render_html(b, Ctx(ticket=t, ws=ws)))  # the page still links it


def test_dashboard_csp_lets_the_page_play_a_linked_video():
    from orch.dashboard.app import PAGE_CSP
    assert "default-src 'self'" in PAGE_CSP and "media-src" not in PAGE_CSP and "img-src 'self' data:" in PAGE_CSP
