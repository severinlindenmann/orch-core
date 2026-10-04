import pytest

pytest.importorskip("fastapi")


def test_repo_filter(dash, put):
    put("open", title="In dbt", repos=["dbt-models"])
    put("open", title="In infra", repos=["platform-infra"])
    html = dash.get("/board?repo=dbt-models").text
    assert "In dbt" in html and "In infra" not in html


def test_external_filter_by_prefix_and_key(dash, put):
    put("open", title="Has key", external=[{"key": "ABC-123", "url": None}])
    put("open", title="No key")
    assert "No key" not in dash.get("/board?external=ABC").text
    assert "Has key" in dash.get("/board?external=abc-123").text


def test_cards_are_draggable_and_columns_have_status(dash, put):
    tid = put("open")
    html = dash.get("/board").text
    assert f'data-ticket="{tid}"' in html and 'draggable="true"' in html
    assert 'data-status="in-progress"' in html


def test_done_hidden_until_requested(dash, put):
    put("done", title="Finished one")
    assert "Finished one" not in dash.get("/board").text
    assert "Finished one" in dash.get("/board?show_done=1").text


def test_external_filter_survives_non_string_key(dash, put):
    put("open", title="Numeric key", external=[{"key": 123, "url": None}])
    r = dash.get("/board?external=abc")
    assert r.status_code == 200 and "Numeric key" not in r.text
    assert "Numeric key" in dash.get("/board?external=123").text


def _card(html, tid):
    import re
    m = re.search(rf'<a class="card tcard" href="/t/{tid}".*?</a>', html, re.S)
    assert m, tid
    return m.group(0)


def test_column_headings_are_h2(dash):
    html = dash.get("/board").text
    assert '<use href="#i-neu"/></svg> Backlog <span class="count">' in html and "<h3>" not in html


def test_stale_claim_chip_says_stale(dash, put):
    tid = put("in-progress", claim={"harness": "claude-code", "session": "s1", "at": "2020-01-01T00:00:00Z"})
    card = _card(dash.get("/board").text, tid)
    # the move chip (ticket_card): the agent's name sits inside the stale chip, never in pink
    assert "Stale · claude-code" in card and "working" not in card.lower()
    assert 'class="chip chip-warn"><svg class="i" aria-hidden="true"><use href="#i-warn"/></svg> Stale · claude-code' in card


def test_claimed_testing_ticket_chip_says_waits_for_you(dash, put):
    from orch.clock import stamp
    tid = put("testing", sections={"Verification": "ok"}, claim={"harness": "copilot", "session": "s2", "at": stamp()})
    card = _card(dash.get("/board").text, tid)
    assert '<span class="chip chip-you"><svg class="i" aria-hidden="true"><use href="#i-you"/></svg> Verdict</span>' in card  # your move, pink
    assert "copilot" in card and "chip-you" not in card.split("Verdict</span>", 1)[1]  # the agent line is not pink


def test_working_claim_chip(dash, put):
    from orch.clock import stamp
    tid = put("in-progress", claim={"harness": "claude-code", "session": "s3", "at": stamp()})
    card = _card(dash.get("/board").text, tid)  # M: initials with the name as tooltip and screen-reader text
    assert 'title="claude-code" aria-hidden="true">CC</span>' in card and '<span class="sr-only">claude-code: </span>' in card


def test_claim_without_harness_never_says_none(dash, put):
    from orch.clock import stamp
    tid = put("in-progress", claim={"harness": None, "session": "s4", "at": stamp()})
    card = _card(dash.get("/board").text, tid)
    assert "None" not in card and '<span class="sr-only">agent: </span>' in card


def test_unclaimed_testing_ticket_shows_verdict_chip(dash, put):
    tid = put("testing", sections={"Verification": "ok"})
    assert '<span class="chip chip-you"><svg class="i" aria-hidden="true"><use href="#i-you"/></svg> Verdict</span>' in _card(dash.get("/board").text, tid)


def test_broken_card_has_no_stray_separator(dash, ws, put):
    from orch.core import store
    tid = put("open")
    store.resolve(ws, tid).path.write_text("---\nnot: [valid\n---\n", encoding="utf-8")
    card = _card(dash.get("/board").text, tid)
    assert '<span class="meta"> · </span>' not in card and "> · " not in card
