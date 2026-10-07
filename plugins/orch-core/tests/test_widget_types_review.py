"""Core widget types for reading a deliverable and giving a verdict (issue #202): gallery preview review summary."""
import hashlib
import json

import pytest

from orch.core.model import Ticket
from orch.widgets import Ctx, check_ticket, registry, render_document, render_html, render_text

NAMES = ["gallery", "preview", "review", "summary"]
F = "```"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
HTML = b"<!doctype html><p>landing</p>"


def _ticket(body, ac="- [ ] first\n- [ ] second"):
    return Ticket(meta={"id": "L-0001", "title": "x"},
                  sections={"Acceptance criteria": ac, "Verification": f"- AC1: ok\n\n{F}orch\n{json.dumps(body)}\n{F}"})


def block(body, ws=None, **kw):
    t = _ticket(body, **kw)
    [b] = check_ticket(t, ws=ws)
    return t, b


@pytest.fixture
def art(ws):
    folder = ws.artifacts_dir / "L-0001"
    folder.mkdir(parents=True)
    (folder / "a.png").write_bytes(PNG)
    (folder / "page.html").write_bytes(HTML)
    (folder / "poster.png").write_bytes(PNG + b"1")
    return {n: hashlib.sha256((folder / n).read_bytes()).hexdigest() for n in ("a.png", "page.html", "poster.png")}


def pin(name, sha):
    return {"path": f"artifacts/L-0001/{name}", "sha256": sha}


@pytest.mark.parametrize("name", NAMES)
def test_example_validates_renders_and_has_text(name):
    mod = registry.core_types()[name]
    t, b = block({**mod.EXAMPLE, "title": "T"})
    assert b.problems == [] or [p.level for p in b.problems] == ["warning"] * len(b.problems)  # a digest of zeros
    html = str(render_html(b, Ctx(ticket=t)))
    assert f"w-t-{name}" in html and "<script" not in html
    assert render_text(b, Ctx(ticket=t)).startswith("[T · core]\n")


def test_the_four_are_listed_with_their_moments():
    types = registry.core_types()
    assert {n: types[n].MOMENT for n in NAMES} == {"gallery": "review", "preview": "review", "review": "verify", "summary": "understand"}


def test_gallery_images_open_in_a_lightbox_and_files_link(ws, art):
    t, b = block({"type": "gallery", "columns": 3, "items": [
        {"label": "Post 01", **pin("a.png", art["a.png"])},
        {"label": "Landing A", **pin("page.html", art["page.html"]), "poster": pin("poster.png", art["poster.png"])},
        {"label": "Landing B", **pin("page.html", art["page.html"])}]}, ws=ws)
    assert b.problems == []
    html = str(render_html(b, Ctx(ticket=t, ws=ws)))
    assert f'data-lightbox="Post 01"' in html and f'src="/a/L-0001/a.png?v={art["a.png"]}"' in html
    assert f'href="/a/L-0001/page.html?v={art["page.html"]}" target="_blank" rel="noopener"' in html
    assert f'src="/a/L-0001/poster.png?v={art["poster.png"]}"' in html  # the poster stands in for the page
    assert html.count('<span class="w-gal-type">HTML</span>') == 1  # no poster: the file type
    assert "--cols:3" in html and "data-lightbox=\"Landing A\"" not in html  # a file is a link, never a lightbox image


def test_gallery_refuses_a_changed_file_and_embeds_images_in_a_document(ws, art):
    t, b = block({"type": "gallery", "items": [{"label": "x", **pin("a.png", "0" * 64)}]}, ws=ws)
    assert [p.code for p in b.problems] == ["widget-digest"]
    assert "cannot be shown" in str(render_html(b, Ctx(ticket=t, ws=ws)))
    t, ok = block({"type": "gallery", "items": [{"label": "x", **pin("a.png", art["a.png"])},
                                                 {"label": "p", **pin("page.html", art["page.html"])}]}, ws=ws)
    doc = render_document(ok, Ctx(ticket=t, ws=ws))
    assert 'src="data:image/png;base64,' in doc and "/a/L-0001/page.html" not in doc and "<script" not in doc


def test_gallery_rejects_bad_shapes():
    for body in ({"type": "gallery", "items": []}, {"type": "gallery", "columns": 1, "items": [{"label": "x", **pin("a.png", "0" * 64)}]},
                 {"type": "gallery", "items": [{"path": "artifacts/L-0001/a.png", "sha256": "0" * 64}]}):
        assert block(body)[1].problems, body


def test_preview_frames_a_pinned_local_page_without_any_sandbox_flag(ws, art):
    t, b = block({"type": "preview", **pin("page.html", art["page.html"]), "viewports": [390, 1280], "height": 300}, ws=ws)
    assert b.problems == []
    html = str(render_html(b, Ctx(ticket=t, ws=ws)))
    assert f'<iframe class="w-pv-frame" sandbox="" referrerpolicy="no-referrer"' in html
    assert f'src="/a/L-0001/page.html?v={art["page.html"]}"' in html and "width:1280px;height:300px" in html
    assert html.count('class="w-pv-size"') == 2 and 'data-w="390"' in html and "allow-scripts" not in html


def test_preview_needs_a_page_and_never_frames_a_url_or_a_changed_file(ws, art):
    t, b = block({"type": "preview", "url": "https://claude.ai/artifact/abc", "label": "Draft"}, ws=ws)
    html = str(render_html(b, Ctx(ticket=t, ws=ws)))
    assert "<iframe" not in html and 'href="https://claude.ai/artifact/abc"' in html
    t, b = block({"type": "preview", **pin("page.html", "0" * 64)}, ws=ws)
    assert "<iframe" not in str(render_html(b, Ctx(ticket=t, ws=ws)))
    t, b = block({"type": "preview", **pin("a.png", art["a.png"])}, ws=ws)  # an image is not a page
    assert "<iframe" not in str(render_html(b, Ctx(ticket=t, ws=ws)))
    t, b = block({"type": "preview"}, ws=ws)
    assert "either a pinned file" in str(render_html(b, Ctx(ticket=t, ws=ws)))
    t, b = block({"type": "preview", "url": "https://x.test", **pin("page.html", art["page.html"])}, ws=ws)
    assert "either a pinned file" in str(render_html(b, Ctx(ticket=t, ws=ws)))


def test_preview_in_a_standalone_document_is_not_framed(ws, art):
    t, b = block({"type": "preview", **pin("page.html", art["page.html"])}, ws=ws)
    assert "<iframe" not in render_document(b, Ctx(ticket=t, ws=ws))


def test_review_lists_the_tickets_own_criteria():
    t, b = block({"type": "review"})
    html = str(render_html(b, Ctx(ticket=t)))
    assert 'data-ac="1"' in html and 'data-ac="2"' in html and "first" in html and "second" in html
    assert html.count('type="radio"') == 6 and "data-review-send" in html
    t, b = block({"type": "review", "acs": [2]})
    html = str(render_html(b, Ctx(ticket=t)))
    assert 'data-ac="2"' in html and 'data-ac="1"' not in html
    assert render_text(b, Ctx(ticket=t)).endswith("\nAC2: second")


def test_review_says_so_when_there_are_no_criteria():
    t, b = block({"type": "review"}, ac="")
    assert "no acceptance criteria to review" in str(render_html(b, Ctx(ticket=t)))


def test_review_escapes_criteria_text():
    t, b = block({"type": "review"}, ac="- [ ] <script>alert(1)</script>")
    assert "<script" not in str(render_html(b, Ctx(ticket=t)))


def test_summary_fields_text_and_limits():
    t, b = block({"type": "summary", "delivered": "ten **drafts**", "check_first": "open it", "open": ["a", "b"], "not_done": "scheduling"})
    html = str(render_html(b, Ctx(ticket=t)))
    assert "<dt>Delivered</dt>" in html and "<strong>drafts</strong>" in html and html.count("w-r-warn") == 2
    assert render_text(b, Ctx(ticket=t)).splitlines()[1:] == ["Delivered: ten **drafts**", "Check first: open it",
                                                              "Open points: a; b", "Not done: scheduling"]
    assert block({"type": "summary", "delivered": "x" * 401, "check_first": "y"})[1].problems
    assert block({"type": "summary", "delivered": "x"})[1].problems  # check_first is required
    assert block({"type": "summary", "delivered": "x", "check_first": "y", "open": ["z"] * 9})[1].problems


def test_summary_text_stays_inert():
    t, b = block({"type": "summary", "delivered": "<img src=x onerror=1>", "check_first": "[x](javascript:alert(1))"})
    html = str(render_html(b, Ctx(ticket=t)))
    assert "<img" not in html and 'href="javascript' not in html
