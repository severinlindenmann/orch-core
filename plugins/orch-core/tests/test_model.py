import pytest

from orch.core.model import new_ticket, parse_ticket, render_ticket
from orch.errors import TicketParseError


def _t():
    return new_ticket("L-0042", "Back up nightly config", type="feature", priority="normal", size="m", created="2026-09-30T08:12Z")


def test_new_ticket_scaffold():
    t = _t()
    assert t.status == "backlog" and t.id == "L-0042"
    assert t.sections == {}  # anatomy v2: sections appear when written
    assert t.meta["gates"]["requirements"] == {"approved": None, "via": None, "hash": None}


def test_render_parse_roundtrip():
    t = _t()
    t.set_section("Ask", "Please back it up.\n\nThanks")
    t.append_log("- 2026-09-30T08:12Z [you] created")
    text = render_ticket(t)
    again = parse_ticket(text)
    assert again.meta == t.meta and again.sections == t.sections
    assert render_ticket(again) == text
    assert text.startswith("---\nid: L-0042\n")
    assert "# L-0042 — Back up nightly config" in text


def test_timestamps_stay_strings():
    t = parse_ticket("---\nid: L-1\ntitle: x\ncreated: 2026-09-30T08:12Z\nupdated: 2026-09-30 08:12:00\n---\n")
    assert t.meta["created"] == "2026-09-30T08:12Z"
    assert t.meta["updated"] == "2026-09-30 08:12:00"


def test_crlf_and_bom():
    t = parse_ticket("﻿---\r\nid: L-1\r\ntitle: Win\r\n---\r\n\r\n## Ask\r\n\r\nline one\r\nline two\r\n")
    assert t.section("Ask") == "line one\nline two"
    assert "\r" not in render_ticket(t)


def test_heading_inside_code_fence_is_content():
    body = "## Plan\n\n```md\n## Not a section\n```\n\nafter\n\n## Log\n\n- x\n"
    t = parse_ticket("---\nid: L-1\ntitle: t\n---\n" + body)
    assert "## Not a section" in t.section("Plan")
    assert t.section("Plan").endswith("after")
    assert "Not a section" not in t.sections


def test_unknown_sections_and_keys_preserved():
    t = parse_ticket("---\nid: L-1\ntitle: t\nx-tix: {id: TIX-17}\n---\n\n## Custom notes\n\nkeep me\n\n## Ask\n\nhi\n")
    out = render_ticket(t)
    assert "x-tix:" in out and "## Custom notes\n\nkeep me" in out
    assert out.index("## Ask") < out.index("## Custom notes")


def test_yes_string_survives():
    t = _t()
    t.meta["questions"] = [{"id": "Q1", "answer": "yes"}]
    assert parse_ticket(render_ticket(t)).meta["questions"][0]["answer"] == "yes"


def test_preamble_kept_and_first_h1_regenerated():
    t = parse_ticket("---\nid: L-1\ntitle: New title\n---\n\n# L-1 — Old title\n\nFree text before sections.\n\n## Ask\n\nq\n")
    assert t.preamble == "Free text before sections."
    assert "# L-1 — New title" in render_ticket(t)


@pytest.mark.parametrize("text", [
    "no frontmatter",
    "---\nid: L-1\n",
    "---\n: bad: yaml: [\n---\n",
    "---\ntitle: no id\n---\n",
    "---\n- a list\n---\n",
])
def test_parse_errors(text):
    with pytest.raises(TicketParseError):
        parse_ticket(text)


# -- libyaml loader (perf): same result as the pure-Python SafeLoader --------------------------------------------

_PARITY_DOCS = [
    "id: L-1\ntitle: x\ncreated: 2026-09-30T08:12Z\nupdated: 2026-09-30 08:12:00\nday: 2026-09-30\n",
    "id: L-2\nflags: [yes, no, on, off, true, False, ~, null]\nnums: [0o17, 017, 0x1f, 1e3, 1_000, .inf, -.nan, 3.0]\n",
    "id: L-3\ntitle: \"Grüße — 😀 \\u00e9\"\ntext: |\n  line one\n  line two\nfold: >-\n  folded\n  text\n",
    "id: L-4\nbase: &b {a: 1, b: [1, 2]}\nref: *b\nmerged:\n  <<: *b\n  c: 3\n",
    "id: L-5\nquestions:\n- id: Q1\n  options: [{key: a, label: 'it''s'}]\n  answer: null\nexternal: []\nbranches: {}\n",
    "id: '0042'\ntitle: 12:30\nver: 1.10\nempty:\nkey with spaces: v\n",
]


@pytest.mark.parametrize("doc", _PARITY_DOCS)
def test_fast_loader_matches_pure_python_loader(doc):
    import yaml
    from orch.core import model
    pure = yaml.load(doc, Loader=model._PyLoader)
    assert model.yaml_load(doc) == pure
    assert repr(model.yaml_load(doc)) == repr(pure)  # same types too (e.g. str timestamps, int vs float)


def test_fast_loader_is_libyaml_when_available():
    import yaml
    from orch.core import model
    if getattr(yaml, "__with_libyaml__", False):
        assert issubclass(model._Loader, yaml.CSafeLoader)
    else:
        assert model._Loader is model._PyLoader


def test_fast_loader_matches_on_real_tickets(ws, put):
    import yaml
    from orch.core import model, store
    put("backlog", title="Ä title: with colon", labels=["x", "y"])
    put("in-progress", sections={"Ask": "do it"})
    for e in store.scan(ws):
        text = e.path.read_text(encoding="utf-8")
        fm = text[4:text.index("\n---", 4)]
        assert model.yaml_load(fm) == yaml.load(fm, Loader=model._PyLoader)


def test_invalid_yaml_keeps_the_pure_python_error_text():
    import yaml
    from orch.core import model
    bad = "---\nid: L-1\ntitle: [unclosed\n---\n"
    with pytest.raises(yaml.YAMLError) as pure:
        yaml.load("id: L-1\ntitle: [unclosed\n", Loader=model._PyLoader)
    with pytest.raises(TicketParseError) as got:
        parse_ticket(bad, "x.md")
    assert str(pure.value) in got.value.message
