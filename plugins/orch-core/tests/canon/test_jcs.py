"""Canonical JSON: RFC 8785 test data, protocol-v2 §2.3 vectors, rejections."""

import json
import struct
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.canon import jcs

VECTORS = json.loads((Path(__file__).parent.parent / "vectors" / "vectors_v2.json").read_text())["encodings"]


def _f(bits: int) -> float:
    return struct.unpack(">d", struct.pack(">Q", bits))[0]


# RFC 8785 Appendix B (IEEE-754 bit pattern -> ECMAScript text)
RFC_NUMBERS = [
    (0x0000000000000000, "0"),
    (0x8000000000000000, "0"),
    (0x0000000000000001, "5e-324"),
    (0x8000000000000001, "-5e-324"),
    (0x7FEFFFFFFFFFFFFF, "1.7976931348623157e+308"),
    (0xFFEFFFFFFFFFFFFF, "-1.7976931348623157e+308"),
    (0x4340000000000000, "9007199254740992"),
    (0xC340000000000000, "-9007199254740992"),
    (0x4430000000000000, "295147905179352830000"),
    (0x44B52D02C7E14AF5, "9.999999999999997e+22"),
    (0x44B52D02C7E14AF6, "1e+23"),
    (0x44B52D02C7E14AF7, "1.0000000000000001e+23"),
    (0x444B1AE4D6E2EF4E, "999999999999999700000"),
    (0x444B1AE4D6E2EF4F, "999999999999999900000"),
    (0x444B1AE4D6E2EF50, "1e+21"),
    (0x3EB0C6F7A0B5ED8C, "9.999999999999997e-7"),
    (0x3EB0C6F7A0B5ED8D, "0.000001"),
    (0x41B3DE4355555553, "333333333.3333332"),
    (0x41B3DE4355555554, "333333333.33333325"),
    (0x41B3DE4355555555, "333333333.3333333"),
    (0x41B3DE4355555556, "333333333.3333334"),
    (0x41B3DE4355555557, "333333333.33333343"),
    (0xBECBF647612F3696, "-0.0000033333333333333333"),  # node and CPython agree on this digit string
    (0x43143FF3C1CB0959, "1424953923781206.2"),
]


@pytest.mark.parametrize(("bits", "text"), RFC_NUMBERS)
def test_rfc8785_number_serialisation(bits, text):
    assert jcs.dumps_general(_f(bits)) == text.encode()


def test_rfc8785_section_3_2_2_example():
    # The RFC's input, as JSON text (floats and \\u escapes included), parsed with the stdlib.
    src = r"""{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
     "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
     "literals": [null, true, false]}"""
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert jcs.dumps_general(json.loads(src)) == expected.encode()


def test_rfc8785_utf16_key_sorting():
    obj = {k: 1 for k in ["€", "\r", "דּ", "1", "\U0001f600", "\u0080", "ö"]}
    keys = list(json.loads(jcs.dumps_general(obj), object_pairs_hook=lambda p: p))
    assert [k for k, _ in keys] == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]


def test_utf16_vs_codepoint_order():
    # U+FB33 > U+1F600 by code point, but its UTF-16 unit (0xFB33) > 0xD83D too; use U+E000 vs U+10000:
    obj = {"": 1, "\U00010000": 2}
    assert jcs.dumps_general(obj).decode() == '{"\U00010000":2,"":1}'


def test_general_rejects_nan_inf():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(jcs.JcsError):
            jcs.dumps_general(bad)


@pytest.mark.parametrize("v", VECTORS["canonical_json"], ids=lambda v: v["name"])
def test_protocol_canonical_json_vectors(v):
    assert jcs.dumps(v["value"]).hex() == v["bytes"]


@pytest.mark.parametrize("v", VECTORS["strict_parse"], ids=lambda v: v["name"])
def test_protocol_strict_parse_vectors(v):
    if v["ok"]:
        jcs.loads_strict(v["text"])
    else:
        with pytest.raises(jcs.JcsError):
            jcs.loads_strict(v["text"])


def test_strict_parse_invalid_utf8_and_bytes_input():
    assert jcs.loads_strict(b'{"a":"\xc3\xbc"}') == {"a": "ü"}
    with pytest.raises(jcs.JcsError):
        jcs.loads_strict(b'{"a":"\xff"}')
    with pytest.raises(jcs.JcsError):
        jcs.loads_strict(b"")
    with pytest.raises(jcs.JcsError):
        jcs.loads_strict("{'a':1}")


def test_strict_parse_depth():
    ok = "[" * 16 + "]" * 16
    jcs.loads_strict(ok)
    with pytest.raises(jcs.JcsError):
        jcs.loads_strict("[" * 17 + "]" * 17)
    with pytest.raises(jcs.JcsError):
        jcs.loads_strict("[" * 100000)


@pytest.mark.parametrize(
    "bad",
    [
        1.0,
        float("nan"),
        2**53,
        -(2**53),
        {"": 1},
        {"ä": 1},
        {1: 1},
        {"a": "\ud800"},
        {"a": (1, 2)},
        {"a": b"x"},
        {"a": {1, 2}},
        {"a": object()},
        [[[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]],
    ],
)
def test_dumps_rejects(bad):
    with pytest.raises(jcs.JcsError):
        jcs.dumps(bad)


def test_dumps_accepts_limits():
    assert jcs.dumps([2**53 - 1, -(2**53 - 1)]) == b"[9007199254740991,-9007199254740991]"
    assert jcs.dumps([[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]) == b"[" * 16 + b"]" * 16
    assert jcs.dumps({"a": True, "b": None, "c": False}) == b'{"a":true,"b":null,"c":false}'


def test_bool_is_not_int_and_escapes():
    assert jcs.dumps([True, 1]) == b"[true,1]"
    assert jcs.dumps("\x00\x1f\x7f ") == '"\\u0000\\u001f\x7f "'.encode()


_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**53 - 1), 2**53 - 1),
    st.text(st.characters(codec="utf-8")),
)
_json = st.recursive(
    _scalars,
    lambda c: st.one_of(
        st.lists(c, max_size=4), st.dictionaries(st.text(st.characters(codec="ascii"), min_size=1), c, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=100, deadline=None)
@given(_json)
def test_roundtrip_idempotent_and_matches_python_json(obj):
    try:
        out = jcs.dumps(obj)
    except jcs.JcsError:
        return  # only depth can trigger it here
    assert jcs.loads_strict(out) == obj
    assert jcs.dumps(jcs.loads_strict(out)) == out
    ref = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert out == ref
