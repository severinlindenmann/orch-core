"""Direct public API and adversarial reader checks."""

import sys

import pytest

from orch.widgets import (
    CORE_TYPES,
    MAX_BLOCK_BYTES,
    MAX_DEPTH,
    PROPOSED_TYPES,
    WIDGET_SECTIONS,
    parse_block,
    parse_section,
    resolve_ticket_widgets,
    segments_to_json,
    spec_to_json,
    strict_json,
)


def test_public_api():
    r = parse_block('{"type":"bars","id":"sizes","data":{"a":1}}')
    assert r.reason is None
    assert spec_to_json(r.spec) == {"layer": "type", "id": "sizes", "type": "bars", "fields": {"data": {"a": 1}}}
    assert type(r.spec["fields"]["data"]["a"]) is float
    assert len(CORE_TYPES) == 12
    assert PROPOSED_TYPES == ("timeline", "progress")
    assert WIDGET_SECTIONS == {"context", "current_state", "verification"}
    text = '```orch\n{"type":"kv","items":{"a":true}}\n```'
    assert parse_section(text, "context")[0].block.line == 1
    resolved = segments_to_json(resolve_ticket_widgets({"context": text}))
    assert resolved["context"][0]["block"]["index"] == 0


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("{}", "block needs exactly one of type, widget or html"),
        ('{"type":"bars","data":{"a":true}}', 'value of "a" must be a number'),
        ('{"a":1,"a":2}', 'invalid JSON: duplicate key "a"'),
        ("1e400", "invalid JSON: NaN and Infinity are not allowed"),
        (" " * (MAX_BLOCK_BYTES + 1), "block is larger than 64 KiB"),
        ("[" * 100000, "block is larger than 64 KiB"),
        ("[" * 1000, "invalid JSON: nesting deeper than 8 levels"),
        (None, "block could not be read"),
    ],
)
def test_refusals_never_raise(raw, reason):
    assert parse_block(raw).reason == reason


def test_depth_is_bounded_before_recursing():
    depths = []

    def trace(frame, event, arg):
        if (
            event == "call"
            and frame.f_code.co_name == "value"
            and frame.f_globals.get("__name__") == "orch.widgets.strict"
        ):
            depth = 0
            while frame:
                if frame.f_code.co_name == "value":
                    depth += 1
                frame = frame.f_back
            depths.append(depth)
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(ValueError, match="nesting deeper than 8 levels"):
            strict_json("[" * 100000)
    finally:
        sys.settrace(previous)
    assert max(depths) == MAX_DEPTH + 1
    assert len(depths) == MAX_DEPTH + 1


def test_utf16_offsets_and_lengths():
    assert parse_block('"😀"x').reason == 'invalid JSON: unexpected "x" at position 4'
    assert parse_block('{"type":"callout","role":"info","text":"' + "😀" * 1000 + '"}').reason is None
    assert (
        parse_block('{"type":"callout","role":"info","text":"' + "😀" * 1001 + '"}').reason
        == "text must be 1 to 2000 characters"
    )
