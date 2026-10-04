import re

import pytest

pytest.importorskip("fastapi")

PAGES = ["/", "/board", "/board?view=list", "/activity", "/reports", "/widgets", "/workspace?tab=widgets", "/workspace", "/new"]


@pytest.mark.parametrize("url", PAGES)
def test_one_h1_and_labelled_controls(dash, url):
    html = dash.get(url).text
    assert len(re.findall(r"<h1\b", html)) == 1
    for m in re.finditer(r"<(input|select|textarea)\b([^>]*)>", html):
        attrs = m.group(2)
        if 'type="hidden"' in attrs or 'type="radio"' in attrs or 'type="checkbox"' in attrs:
            continue
        ident = re.search(r'id="([^"]+)"', attrs)
        assert "aria-label=" in attrs or (ident and f'for="{ident.group(1)}"' in html) or "<label" in html, (url, attrs)


@pytest.mark.parametrize("url", PAGES)
def test_menu_has_no_underline_rule(dash, url):
    css = dash.get("/static/app.css").text
    assert re.search(r"\.menu a[^{]*\{[^}]*text-decoration:\s*none", css)


def test_ticket_page_a11y(dash, put):
    tid = put("in-progress", sections={"Plan": "- [x] a\n- [ ] b"})
    html = dash.get(f"/t/{tid}").text
    assert len(re.findall(r"<h1\b", html)) == 1 and html.count('aria-current="step"') == 1
    assert 'class="sr-only"' in html


def test_no_status_without_icon(dash, put):
    put("testing", sections={"Verification": "ok"}); put("open"); put("in-progress")
    for url in ("/", "/board", "/activity"):
        for m in re.finditer(r'<span class="chip chip-(ok|info|you|warn|err|neu)">(.*?)</span>', dash.get(url).text, re.S):
            assert 'aria-hidden="true"' in m.group(2), (url, m.group(0))


def _phone_css(css):
    return "".join(re.findall(r"@media \(max-width: 720px\) \{(.*?)\n\}", css, re.S))


def test_phone_rules_stack_tables_and_tiles(dash):
    css = dash.get("/static/app.css").text
    phone = _phone_css(css)
    # widget tables (.wtable) stack by container width instead (test_ds_render.py)
    assert re.search(r"\.table:not\(\.wtable\) tr \{[^}]*display: flex", phone)
    assert re.search(r"\.table:not\(\.wtable\) thead \{[^}]*position: absolute", phone)
    assert re.search(r"\.summary \{[^}]*repeat\(2,", phone)
    # A visually hidden header cell must scroll inside its table, never widen the page.
    assert re.search(r"\.table-wrap \{[^}]*position: relative", css)
    # Link chips >= 24 px; selects in Start agent >= 44 px like every other control.
    assert re.search(r"\.lnk, \.chip-link[^{]*\{[^}]*min-height: 24px", css)
    assert re.search(r"\.sa-field select \{[^}]*min-height: 44px", css)


def test_stacked_rows_have_labels_and_two_chips_at_most(dash, put):
    put("in-progress"); put("open")
    html = dash.get("/board?view=list").text
    assert 'data-label="Updated"' in html
    for row in re.findall(r"<tr>(.*?)</tr>", html.split("<tbody>", 1)[1], re.S):
        shown = [c for c in re.findall(r"<td(?![^>]*ph-hide)[^>]*>(.*?)</td>", row, re.S)]
        assert sum(c.count('class="chip ') for c in shown) <= 2, row


_UNLABELLED_OK = ("wrap", "cell-title", "cell-action", "empty")


def test_every_stacked_cell_has_a_label(dash, ws, put, aops, monkeypatch):
    """On a phone the header row is hidden, so every data cell of a stacked table names itself via
    data-label, except the title and action cells (and the one-cell empty row) by design."""
    import json
    from types import SimpleNamespace

    from orch.clock import stamp_s
    from orch.core.events import last_seq
    from orch.dashboard import routes_workspace

    claimed = put("in-progress", title="Claimed"); aops.claim(claimed)
    put("open", priority="high", external=[{"key": "ABC-1"}])
    done = put("done", title="Done")
    events = ws.state_dir / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a", encoding="utf-8") as f:
        # the next seq: a line that skips ahead is ignored as tampered
        f.write(json.dumps({"seq": last_seq(ws) + 1, "at": stamp_s(), "ticket": done, "kind": "ticket.moved", "actor": "human:you",
                            "via": "cli", "data": {"from": "testing", "to": "done"}}) + "\n")
    monkeypatch.setattr(routes_workspace, "run_checks",
                        lambda ws, **kw: [SimpleNamespace(level="warning", ticket=claimed, code="some-code", message="m")])
    monkeypatch.setattr(routes_workspace, "_addon_rows",
                        lambda ws, runtime: [{"name": "x", "title": "X", "kind": "custom", "version": "1", "source": "/src/x",
                                              "trust": "trusted", "trust_role": "ok", "trust_label": "trusted",
                                              "enabled": True, "can_enable": True, "health": None, "review": None,
                                              "fields": [], "settings_groups": [], "update": None, "error": None,
                                              "problem": None}])
    for url in ("/board?view=list", "/activity", "/reports", "/workspace"):
        html = dash.get(url).text
        tables = re.findall(r'<table class="table[^"]*">(.*?)</table>', html, re.S)
        cells = [m for t in tables for m in re.findall(r"<td\b([^>]*)>", t)]
        assert any("data-label=" in c for c in cells), url  # real data rows, not only the empty row
        for attrs in cells:
            cls = re.search(r'class="([^"]*)"', attrs)
            if cls and set(cls.group(1).split()) & set(_UNLABELLED_OK):
                continue
            assert "data-label=" in attrs, (url, attrs)
