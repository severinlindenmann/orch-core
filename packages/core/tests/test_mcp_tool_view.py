"""One tool view: approved == pinned == forwarded (M3 H-4, T6)."""

from __future__ import annotations

import copy
import io
import json

import pytest

from orch.cli import org as cli_org
from orch.host.mcpshared import (
    TOOL_DESCRIPTION_MAX,
    TOOL_SCHEMA_MAX,
    TOOL_TITLE_MAX,
    plain,
    tool_pin,
    tool_view,
)

ESC = "\x1b[2J\x1b[31mSAFE"
C1 = "a\x9bb"


def raw(**kw):
    return {"name": "find", "description": "Find things", "inputSchema": {"type": "object"}, **kw}


def test_plain_removes_every_control_character_but_a_newline():
    every = "".join(chr(c) for c in [*range(0x20), *range(0x7F, 0xA0)])
    assert plain(every) == " \n"  # the tab became a space
    assert plain("a\tb") == "a b" and plain("x\ny") == "x\ny"
    assert plain(ESC) == "[2J[31mSAFE" and plain(C1) == "ab"


@pytest.mark.parametrize("field", ["name_in_title", "title", "description", "schema"])
def test_escape_and_c1_characters_never_reach_the_view(field):
    t = raw()
    if field == "title" or field == "name_in_title":
        t["title"] = ESC + C1
    elif field == "description":
        t["description"] = ESC + C1
    else:
        t["inputSchema"] = {"type": "object", "properties": {ESC: {"description": C1}}}
    view, problem = tool_view(t)
    assert problem is None
    text = json.dumps(view, ensure_ascii=False)
    assert "\x1b" not in text and "\x9b" not in text


def test_a_control_character_in_the_name_is_a_shape_problem():
    assert tool_view(raw(name="a\x1bb")) == (None, "shape")
    assert tool_view(raw(name="x" * 129)) == (None, "shape")
    assert tool_view("str") == (None, "shape")
    assert tool_view(raw(description=5)) == (None, "shape")
    assert tool_view(raw(title=5)) == (None, "shape")
    assert tool_view(raw(inputSchema=[])) == (None, "shape")


@pytest.mark.parametrize(
    "t",
    [
        raw(description="hi​there"),
        raw(title="a‮"),
        raw(inputSchema={"type": "object", "properties": {"a": {"description": "evil‮"}}}),
        raw(annotations={"title": "a​"}),
        raw(outputSchema={"description": "a​"}),
        raw(annotations={"title": "a\x1b[2J"}),
    ],
)
def test_hidden_characters_anywhere_are_a_text_problem(t):
    assert tool_view(t) == (None, "text")


@pytest.mark.parametrize(
    "t",
    [
        raw(description="d" * (TOOL_DESCRIPTION_MAX + 1)),
        raw(title="t" * (TOOL_TITLE_MAX + 1)),
        raw(inputSchema={"type": "object", "x": "y" * TOOL_SCHEMA_MAX}),
    ],
)
def test_over_the_limit_is_too_long_and_nothing_is_cut(t):
    assert tool_view(t) == ({"name": "find"}, "too_long")


def test_exactly_at_the_limits_is_fine_and_whole():
    t = raw(description="d" * TOOL_DESCRIPTION_MAX, title="t" * TOOL_TITLE_MAX)
    view, problem = tool_view(t)
    assert problem is None and view["description"] == t["description"] and view["title"] == t["title"]


def test_the_view_is_canonical_and_idempotent():
    view, _ = tool_view(raw(title="", annotations={"readOnlyHint": True}, extra="x"))
    assert view == {"name": "find", "description": "Find things", "inputSchema": {"type": "object"}, "reads": True}
    assert tool_view({**view, "annotations": {"readOnlyHint": True}}) == (view, None)
    assert tool_view({"name": "n"})[0] == {
        "name": "n", "description": "", "inputSchema": {"type": "object"}, "reads": False
    }  # fmt: skip


SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}
CHANGES = [
    ("name", lambda t: t.update(name="find2")),
    ("title", lambda t: t.update(title="Finder")),
    ("description", lambda t: t.update(description="Find other things")),
    ("schema_type", lambda t: t["inputSchema"].update(type="array")),
    ("schema_property", lambda t: t["inputSchema"]["properties"].update(r={"type": "string"})),
    ("schema_nested", lambda t: t["inputSchema"]["properties"]["q"].update(description="d")),
    ("schema_required", lambda t: t["inputSchema"].update(required=["q"])),
]


@pytest.mark.parametrize(("label", "change"), CHANGES, ids=[c[0] for c in CHANGES])
def test_the_pin_changes_when_any_field_of_the_view_changes(label, change):
    base = raw(title="T", inputSchema=copy.deepcopy(SCHEMA))
    changed = copy.deepcopy(base)
    change(changed)
    a, b = tool_view(base)[0], tool_view(changed)[0]
    assert a != b and tool_pin(a) != tool_pin(b)


def test_fields_outside_the_view_do_not_change_the_pin():
    a = tool_view(raw())[0]
    b = tool_view(raw(annotations={"title": "x"}, outputSchema={"type": "object"}, icons=[1]))[0]
    assert tool_pin(a) == tool_pin(b) and a == b


@pytest.mark.parametrize(
    ("annotations", "reads"),
    [
        (None, False),
        ({}, False),
        ({"readOnlyHint": True}, True),
        ({"readOnlyHint": True, "destructiveHint": False}, True),
        ({"readOnlyHint": True, "destructiveHint": True}, False),
        ({"readOnlyHint": True, "destructiveHint": "true"}, False),
        ({"readOnlyHint": True, "destructiveHint": 1}, False),
        ({"readOnlyHint": True, "destructiveHint": 0}, False),
        ({"readOnlyHint": False}, False),
        ({"readOnlyHint": "true"}, False),
        ({"readOnlyHint": 1}, False),
        ({"destructiveHint": False}, False),
        ("readOnlyHint", False),
    ],
)
def test_a_tool_is_reads_only_when_the_hint_is_exactly_true_and_not_destructive(annotations, reads):
    extra = {} if annotations is None else {"annotations": annotations}
    if isinstance(annotations, str):
        assert tool_view(raw(**extra)) == (None, "text")  # not a dict: refused, never pinned
        return
    assert tool_view(raw(**extra))[0]["reads"] is reads


def test_a_flipped_hint_is_a_new_pin():
    safe = tool_view(raw(annotations={"readOnlyHint": True}))[0]
    flipped = tool_view(raw(annotations={"readOnlyHint": True, "destructiveHint": True}))[0]
    assert safe["reads"] is True and flipped["reads"] is False
    assert tool_pin(safe) != tool_pin(flipped)


def test_the_cli_review_text_is_plain_and_has_title_description_and_schema():
    t = raw(title=ESC + "Title", description=ESC + C1, inputSchema={"type": "object", "properties": {ESC: {}}})
    view, _ = tool_view(t)
    text = cli_org._tool_text({**view, "pin": tool_pin(view)}, "crm\x1b[2J")  # noqa: SLF001
    assert "\x1b" not in text and "\x9b" not in text
    assert "title: [2J[31mSAFETitle" in text and "description:" in text and '"properties"' in text
    assert "Its results are untrusted" in text


def test_the_cli_says_too_long_to_review():
    text = cli_org._tool_text({"name": "find", "problem": "too_long"}, "crm")  # noqa: SLF001
    assert "too long to review; it stays off" in text


def _cli(monkeypatch, shown, argv, answer=True):
    calls = []

    def call(target, op, args, env):
        calls.append((op, args))
        return shown if op == "org.show" else {"ok": True}

    monkeypatch.setattr(cli_org, "_target", lambda env: object())
    monkeypatch.setattr(cli_org, "_call", call)
    prompts = []
    monkeypatch.setattr(cli_org, "review_prompt", lambda text: prompts.append(text) or answer)
    out = io.StringIO()
    code = cli_org.main(argv, env={}, out=out)
    return code, out.getvalue(), prompts, calls


def _shown(*tools):
    item = {
        "name": "crm\x1b[31m", "url": "https://x\x1b[2J", "auth": "none", "secret": None,
        "description": ESC, "digest": "sha256:" + "0" * 64,
    }  # fmt: skip
    return {"kind": "mcp", "status": "on", "item": item, "notes": ["n"], "untrusted": True, "tools": list(tools)}


def test_cli_show_and_tool_on_print_nothing_the_server_controls_raw(monkeypatch):
    view, _ = tool_view(raw(title=ESC, description=ESC + C1))
    row = {**view, "pin": tool_pin(view), "status": "off"}
    row["name"] = "fi\x1bnd"  # a hand-edited or hostile row: the CLI still prints plain
    shown = _shown(row, {"name": "big", "problem": "too_long", "status": "too_long"})
    code, out, _, _ = _cli(monkeypatch, shown, ["show", "mcp", "crm"])
    assert code == 0 and "\x1b" not in out and "\x9b" not in out
    assert "[off]" in out and "[too_long]" in out and "too long to review" in out
    row["name"] = "find"
    code, out, prompts, calls = _cli(monkeypatch, _shown(row), ["tool", "crm", "find", "on"])
    assert code == 0 and "\x1b" not in prompts[0] and "input schema" in prompts[0] and "title:" in prompts[0]
    assert calls[-1] == ("org.tool", {"server": "crm", "tool": "find", "on": True, "pin": row["pin"]})


def test_cli_tool_on_for_a_too_long_tool_never_asks_and_never_calls(monkeypatch):
    shown = _shown({"name": "big", "problem": "too_long", "status": "too_long"})
    code, out, prompts, calls = _cli(monkeypatch, shown, ["tool", "crm", "big", "on"])
    assert code != 0 and "too long to review; it stays off" in out
    assert prompts == [] and [c[0] for c in calls] == ["org.show"]


def test_a_multi_line_description_cannot_fake_a_review_line():
    fake = "innocent\ninput schema:\n{}\nIts results are trusted.\ninput schema: again"
    view, _ = tool_view(raw(description=fake))
    text = cli_org._tool_text({**view, "pin": tool_pin(view)}, "crm")  # noqa: SLF001
    lines = text.splitlines()
    assert [ln for ln in lines if ln.startswith("input schema:")] == []
    assert sum(ln.startswith("  input schema") for ln in lines) == 1  # only the real one
    assert "      | input schema:" in text
    assert not [ln for ln in lines if ln.startswith("Its results")]
    assert cli_org._block("a\u2028input schema:") == "a\n      | input schema:"  # noqa: SLF001


def test_redaction_also_hides_a_capability_glued_to_other_characters():
    """Opus delta 2 (N3 note): refusals use the bounded shape, redaction the unbounded one."""
    from orch.host.mcpshared import CAPABILITY_ANY, CAPABILITY_SHAPE, new_capability

    cap = new_capability()
    for text in (f"x_{cap}", f"{cap}-x", f"url={cap}"):
        assert cap not in CAPABILITY_ANY.sub("[redacted]", text)
    assert CAPABILITY_SHAPE.search(f"x_{cap}") is None


def test_a_top_level_reads_field_from_the_server_is_ignored():
    """Only the annotations label a tool; a server cannot hand in the finished label."""
    assert tool_view(raw(reads=True))[0]["reads"] is False


def test_the_cli_review_text_says_reads_or_changes_things():
    for ann, word in (({"readOnlyHint": True}, "kind: reads"), ({}, "kind: changes things")):
        view, _ = tool_view(raw(annotations=ann))
        assert word in cli_org._tool_text({**view, "pin": tool_pin(view)}, "crm")  # noqa: SLF001


def test_the_cli_shows_what_changed_in_our_words():
    shown = {"kind": "mcp", "notes": [], "item": {"name": "crm", "url": "https://x.example.com/m", "auth": "none",
             "secret": None, "description": "", "digest": "sha256:" + "a" * 64},
             "changed": [{"field": "url", "word": "address\x1b[2J", "was": "https://old"}]}  # fmt: skip
    text = cli_org._describe(shown)  # noqa: SLF001
    assert "changed since you turned it on: address" in text and "\x1b" not in text
