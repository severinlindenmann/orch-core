"""Write-time widget checks (section set), the dry run (widgets check) and the type list (widgets types)."""

from __future__ import annotations

import json

import pytest

from orch.widgets import catalog, check, parse_block

GOOD = '```orch\n{"type":"callout","role":"info","text":"hello"}\n```'
BAD = '```orch\n{"type":"series","points":[[1,2]]}\n```'


@pytest.mark.parametrize("name", list(catalog.TYPES))
def test_every_catalog_example_is_valid(name):
    r = parse_block(json.dumps(catalog.TYPES[name][2]))
    assert r.reason is None, r.reason


def test_catalog_lists_every_core_type():
    from orch.widgets import CORE_TYPES

    assert set(catalog.TYPES) == set(CORE_TYPES)


def test_plain_text_is_not_parsed():
    assert check.check_write({}, "plan", "just prose") == []


def test_bad_block_is_refused_with_its_position_and_reason():
    ps = check.check_write({}, "context", "intro\n\n" + GOOD + "\n\n" + BAD)
    assert [(p.block, p.line) for p in ps] == [(2, 7)]
    assert "points needs 2 to 500" in ps[0].reason


@pytest.mark.parametrize("sid", ["summary", "requirements", "out_of_scope", "plan", "decisions", "findings"])
def test_blocks_are_refused_outside_the_widget_sections(sid):
    ps = check.check_write({}, sid, GOOD)
    assert len(ps) == 1 and "not drawn" in ps[0].reason or "not allowed" in ps[0].reason


@pytest.mark.parametrize("sid", ["context", "current_state", "verification"])
def test_blocks_are_accepted_in_the_widget_sections(sid):
    assert check.check_write({}, sid, GOOD) == []


def test_duplicate_id_across_sections_is_refused():
    block = '```orch\n{"type":"callout","id":"a","role":"ok","text":"x"}\n```'
    ps = check.check_write({"verification": block}, "context", block)
    assert len(ps) == 1 and "more than one widget" in ps[0].reason


def test_forty_block_cap_counts_the_whole_ticket():
    many = "\n".join(GOOD for _ in range(40))
    assert check.check_write({}, "context", many) == []
    ps = check.check_write({"verification": GOOD}, "context", many)
    assert len(ps) == 1 and "at most 40" in ps[0].reason
    assert check.check_write({"context": many}, "context", many + "\nmore text") == []  # not worse than before
    assert check.check_section_text({}, "context", many + "\n" + GOOD)  # 41 in one section


def test_templates_and_html_pages():
    tpl = '```orch\n{"widget":"line-chart@1","sha256":"' + "a" * 64 + '"}\n```'
    assert "Widgets add-on" in check.check_write({}, "context", tpl)[0].reason
    assert check.check_write({}, "context", tpl, templates_ok=True) == []
    html = '```orch\n{"html":"artifact:p.html","sha256":"' + "a" * 64 + '"}\n```'
    assert "not available" in check.check_write({}, "context", html, templates_ok=True)[0].reason


def test_message_shows_three_problems_and_the_count_of_the_rest():
    ps = check.check_write({}, "context", "\n".join([BAD] * 5))
    m = check.message(ps, "context")
    assert m.count("widget block") == 3 and "(+2 more)" in m


def test_cap_refusal_names_a_real_line():
    ps = check.check_write({"verification": "\n".join([GOOD] * 39)}, "context", "intro\n\n" + GOOD + "\n\n" + GOOD)
    assert [(p.block, p.line) for p in ps] == [(2, 7)]


def test_hostile_inputs_stay_fast():
    import time

    t = time.monotonic()
    check.check_section_text({}, "context", "~" * 60000 + " x")
    kv = json.dumps({"type": "kv", "items": {str(i): 0 for i in range(7000)}}, separators=(",", ":"))
    for _ in range(6):
        parse_block(kv)
    check.check_section_text({}, "context", "```orch\n" + "[" * 60000 + "\n```")
    assert time.monotonic() - t < 3


def test_a_huge_key_is_not_echoed_back():
    big = "k" * 65000
    for block in (
        json.dumps({"type": "callout", "role": "info", "text": "x", big: 1}),
        f'{{"type":"callout","role":"info","text":"x","{big}":1,"{big}":2}}',
        json.dumps({"type": big}),
        json.dumps({"type": "bars", "data": {"a": 1}, "highlight": big}),
        json.dumps(
            {
                "type": "series",
                "x": ["a", "b"],
                "series": [{"name": big, "values": [1, 2]}, {"name": big, "values": [1, 2]}],
            }
        ),
    ):
        r = parse_block(block[:65000] if len(block) > 65536 else block)
        assert r.reason is not None and len(r.reason) < 400, (len(r.reason or ""), block[:40])
    ps = check.check_write({}, "context", "```orch\n" + json.dumps({"type": big}) + "\n```")
    assert len(check.message(ps, "context")) < 1000
