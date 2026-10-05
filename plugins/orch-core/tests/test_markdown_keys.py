"""Bare ticket keys of this workspace in rendered ticket text link to the ticket (/t/<KEY>): a render rule only, so
the tokens every gate and verdict hash reads stay the same, and nothing changes without a prefix."""
from orch.dashboard.markdown import render_inline, render_markdown

LINK = '<a class="lnk key" href="/t/L-0042">L-0042</a>'


def test_a_bare_key_links():
    assert LINK in render_markdown("see L-0042 first", key_prefix="L")


def test_a_lower_case_key_links_to_the_upper_case_ticket():
    assert 'href="/t/L-0042">l-0042</a>' in render_markdown("see l-0042", key_prefix="L")


def test_inline_text_links_too():
    assert LINK in render_inline("needs L-0042", key_prefix="L")


def test_without_a_prefix_nothing_changes():
    assert render_markdown("see L-0042", key_prefix=None) == render_markdown("see L-0042")
    assert "<a" not in render_markdown("see L-0042")


def test_a_key_inside_a_link_is_not_nested():
    html = render_markdown("[about L-0042](https://example.com/x)", key_prefix="L")
    assert html.count("<a") == 1 and "/t/L-0042" not in html


def test_text_after_a_link_still_links():
    html = render_markdown("[docs](https://example.com) then L-0042", key_prefix="L")
    assert LINK in html


def test_code_and_fences_are_untouched():
    html = render_markdown("`L-0042`\n\n```\nL-0042\n```", key_prefix="L")
    assert "/t/L-0042" not in html


def test_other_prefixes_and_longer_words_stay_text():
    html = render_markdown("GH-1, XL-0042, L-0042x and L-0042-b", key_prefix="L")
    assert "<a" not in html


def test_text_is_still_escaped():
    html = render_markdown("<b>bold</b> L-1", key_prefix="L")
    assert "&lt;b&gt;" in html and "<b>" not in html and '/t/L-1"' in html


def test_the_gate_hash_does_not_move(ws, aops, working):
    from orch.core import gates, store
    t = store.load(ws, working)[1]
    aops.set_section(working, "Requirements", "Builds on L-0042 and L-7")
    t = store.load(ws, working)[1]
    before = gates.gate_hash(t, "requirements")
    render_markdown(t.section("Requirements"), key_prefix="L")
    assert gates.gate_hash(store.load(ws, working)[1], "requirements") == before


def test_the_ticket_page_links_keys(ws, aops, dash):
    other = aops.new("The other one").id
    t = aops.new("Mentions")
    aops.set_section(t.id, "Context", f"Follows {other}.")
    html = dash.get(f"/t/{t.id}").text
    assert f'<a class="lnk key" href="/t/{other}">{other}</a>' in html
