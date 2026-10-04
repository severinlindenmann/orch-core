"""Fix round 4: hidden characters (controls, bidi, zero-width, tags, ...) can neither be decided on nor shown raw."""
import pytest

from orch.textsafe import has_hidden, html_text, strip_hidden, visible

CLASSES = {
    "C0 escape": "\x1b", "carriage return": "\r", "DEL": "\x7f", "C1 NEL": "\x85", "soft hyphen": "­",
    "arabic letter mark": "؜", "mongolian vowel separator": "᠎", "zero width space": "​",
    "right-to-left override": "‮", "isolate": "⁦", "deprecated format": "⁪",
    "interlinear annotation": "￹", "tag character": "\U000e0041", "line separator": " ",
    "paragraph separator": " ", "private use": "", "unassigned": "\U000effff",
    "combining grapheme joiner": "͏", "hangul filler": "ㅤ", "hangul choseong filler": "ᅟ",
    "hangul jungseong filler": "ᅠ", "halfwidth hangul filler": "ﾠ", "BOM": "﻿",
}


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_every_hidden_class_is_caught_and_escaped(name):
    c = CLASSES[name]
    text = f"a{c}b"
    assert has_hidden(text), name
    assert c not in visible(text) and c not in html_text(text) and strip_hidden(text) == "ab"


@pytest.mark.parametrize("text", ["plain", "tab\tand\nnewline", "Grüße, 日本語, emoji 🎉, é (é)"])
def test_ordinary_text_is_not_hidden(text):
    assert not has_hidden(text)


def test_addon_file_names_drop_every_hidden_class():
    from orch.dashboard.addon_files import safe_upload_name as safe_name
    for c in CLASSES.values():
        assert c not in safe_name(f"report{c}.pdf")


def _ready(aops, **sections):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", sections.get("Requirements", "r"))
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    return t.id


def test_approve_refused_on_hidden_characters_every_path(ws, aops, hops, dash):
    from orch.core import gates, store
    from orch.errors import ValidationError
    tid = _ready(aops, Requirements="Back up nightly.‮yliad")
    with pytest.raises(ValidationError, match="hidden or control characters") as e:
        hops.approve(tid, "requirements")
    assert "removes the hidden characters" in e.value.hint
    seen = gates.gate_hash(store.load(ws, tid)[1], "requirements")
    r = dash.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": seen}, follow_redirects=False)
    assert "hidden" in r.headers["location"] and store.load(ws, tid)[1].status == "backlog"


def test_answer_and_verdict_refused_on_hidden_characters(ws, aops, hops, put):
    from orch.errors import ValidationError
    tid = put("open")
    aops.ask(tid, [{"text": "Delete​ the old job?", "type": "confirm"}])
    with pytest.raises(ValidationError, match="hidden"):
        hops.answer(tid, "Q1", "yes")
    other = put("testing", sections={"Verification": "all green\x1b[2K"})
    with pytest.raises(ValidationError, match="hidden"):
        hops.verdict(other, "done")


def test_dashboard_shows_badges_not_raw_characters(dash, ws, aops):
    tid = _ready(aops, Requirements="Back up nightly.‮evil")
    aops.set_section(tid, "Context", "zero​width")
    for page in (dash.get("/groom").text, dash.get(f"/t/{tid}").text):  # a backlog approval: the groom view
        assert "‮" not in page and "​" not in page
        assert "&lt;U+202E&gt;" in page
    assert 'class="hidden-char"' in dash.get(f"/t/{tid}").text


def test_hidden_characters_in_a_title_are_escaped_everywhere(dash, ws, aops):
    t = aops.new("Approve‮ me")
    for page in (dash.get("/board").text, dash.get(f"/t/{t.id}").text):
        assert "‮" not in page and "&lt;U+202E&gt;" in page


# -- fix round 5: references and percent-encoding that Markdown decodes into hidden characters --

ENCODED = {
    "hex reference": "Back up nightly &#x202E;exe.pdf",
    "decimal reference": "Back up nightly &#8238;exe.pdf",
    "named zero width space": "Back&ZeroWidthSpace;up nightly",
    "named soft hyphen": "Back&shy;up nightly",
    "percent-encoded autolink": "Get it from <http://e.com/%E2%80%AEfdp.exe>",
}


@pytest.mark.parametrize("name", sorted(ENCODED))
def test_encoded_hidden_characters_are_badged_on_the_dashboard(dash, ws, aops, name):
    tid = _ready(aops, Requirements=ENCODED[name])
    for page in (dash.get("/").text, dash.get(f"/t/{tid}").text):
        for c in ("‮", "​", "­"):
            assert c not in page, (name, repr(c))
    assert 'class="hidden-char"' in dash.get(f"/t/{tid}").text


@pytest.mark.parametrize("name", sorted(ENCODED))
def test_encoded_hidden_characters_block_decisions(ws, aops, hops, put, name):
    from orch.errors import ValidationError
    tid = _ready(aops, Requirements=ENCODED[name])
    with pytest.raises(ValidationError, match="hidden"):
        hops.approve(tid, "requirements")
    q = put("open")
    aops.ask(q, [{"text": ENCODED[name], "type": "confirm"}])
    with pytest.raises(ValidationError, match="hidden"):
        hops.answer(q, "Q1", "yes")
    v = put("testing", sections={"Verification": ENCODED[name]})
    with pytest.raises(ValidationError, match="hidden"):
        hops.verdict(v, "done")


def test_render_markdown_output_never_holds_hidden_characters():
    from orch.dashboard.markdown import render_inline, render_markdown
    from orch.textsafe import has_hidden
    for text in ENCODED.values():
        assert not has_hidden(render_markdown(text)) and not has_hidden(render_inline(text))


def test_addon_widget_text_links_keys_and_badges_hidden_characters():
    from orch.dashboard.keys import link_keys
    out = str(link_keys("see L-0001‮ now", "L"))
    assert "‮" not in out and "&lt;U+202E&gt;" in out and 'href="/t/L-0001"' in out


def test_close_refused_on_a_hidden_title(ws, aops, hops):
    from orch.errors import ValidationError
    t = aops.new("Duplicate‮ of L-2")
    with pytest.raises(ValidationError, match="hidden"):
        hops.close(t.id, "duplicate")
