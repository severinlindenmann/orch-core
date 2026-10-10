"""Text rules of ticket-format §11.3: NFC (Unicode 16.0), LF, refused controls, bidi and unassigned code points."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.canon import (
    HashError,
    TextError,
    check_text,
    is_clean_text,
    nfc,
    normalize_text,
    section_hash,
    show_invisible,
    suspicious,
)

# anything the store might see: assigned text, controls, bidi, surrogates
_chars = st.characters(codec="utf-8", exclude_categories=())
_texts = st.text(_chars, max_size=40) | st.text(alphabet="e\u0301\u0323\u00e9a\r\n\t\u1100\u1161\u11a8 ", max_size=30)


def test_nfd_to_nfc_and_crs():
    assert normalize_text("e\u0301") == "\u00e9"
    assert normalize_text("\u212b") == "\u00c5"
    assert normalize_text("a\r\nb\rc\nd") == "a\nb\nc\nd"


@pytest.mark.parametrize(
    "s",
    ["trailing  \nspaces\t\n", "no final newline", "\n\nleading blank", "final\n\n\n", "  indented\n", "tab\there", ""],
)
def test_nothing_else_is_touched(s):
    assert normalize_text(s) == s
    assert check_text(s) == s


def test_compat_forms_not_folded_and_ls_ps_are_not_newlines():
    s = "\ufb01 \u2028 \u2029 \u200b"
    assert normalize_text(s) == s


def test_unicode_16_is_pinned_not_the_runtime():
    # Garay (U+10D40) and Todhri/Tulu-Tigalari came with 16.0: accepted whatever the runtime says
    assert check_text("\U00010d40\U00011380") == "\U00010d40\U00011380"
    # U+0378 is unassigned in every version
    with pytest.raises(TextError):
        check_text("\u0378")


def test_controls_tab_and_lf_only():
    for ch in [chr(c) for c in range(0x20) if chr(c) not in "\n\t"] + ["\x7f", "\x80", "\x9f"]:
        with pytest.raises(TextError):
            check_text("a" + ch)
        with pytest.raises(TextError):
            check_text("\u00e9" + ch)  # non-ASCII path
    assert check_text("a\nb\tc") == "a\nb\tc"


def test_one_line():
    with pytest.raises(TextError):
        check_text("a\nb", one_line=True)
    assert check_text("a b\tc", one_line=True) == "a b\tc"


def test_non_str_and_surrogates_are_typed_errors():
    for bad in (b"abc", None, 1):
        with pytest.raises(TextError):
            normalize_text(bad)  # type: ignore[arg-type]
        with pytest.raises(TextError):
            check_text(bad)  # type: ignore[arg-type]
    for s in ("a\ud800", "\udfff", chr(0xD800) + chr(0xDC00)):
        with pytest.raises(TextError):
            check_text(s)
        with pytest.raises(TextError):
            normalize_text(s)
        with pytest.raises(HashError):
            section_hash(s)


def test_is_clean_text():
    assert is_clean_text("abc\n")
    assert not is_clean_text("a\r\n")
    assert not is_clean_text("e\u0301")
    assert not is_clean_text("\x00")
    assert not is_clean_text("\ud800")


def test_suspicious_and_show_invisible():
    s = "a\u200bb\ufeffc\U000e0041\u00ad\ue000"
    got = suspicious(s)
    assert [(x.index, x.codepoint, x.kind) for x in got] == [
        (1, 0x200B, "invisible"),
        (3, 0xFEFF, "invisible"),
        (5, 0xE0041, "invisible"),
        (6, 0xAD, "invisible"),
    ]
    assert show_invisible(s) == "a\u27e8U+200B\u27e9b\u27e8U+FEFF\u27e9c\u27e8U+E0041\u27e9\u27e8U+00AD\u27e9\ue000"
    assert show_invisible("plain") == "plain"
    assert [x.kind for x in suspicious("x\u202ey\u061c")] == ["bidi", "bidi"]
    assert suspicious("clean text \u00e9") == []


@settings(max_examples=150, deadline=None)
@given(_texts)
def test_normalize_idempotent_and_result_is_valid(s):
    try:
        n = normalize_text(s)
    except TextError:
        return  # refused input stays refused; the next test checks it is refused for a stated reason
    assert normalize_text(n) == n
    assert check_text(n) == n
    assert is_clean_text(n)
    assert "\r" not in n


@settings(max_examples=150, deadline=None)
@given(_texts)
def test_check_text_accepts_exactly_the_fixed_points(s):
    try:
        n = normalize_text(s)
    except TextError:
        with pytest.raises(TextError):
            check_text(s)
        return
    assert is_clean_text(s) == (n == s)


def test_error_names_the_first_offending_index_deterministically():
    s = "ab\u0378\u0381\u202e"
    for _ in range(3):
        with pytest.raises(TextError, match="index 2"):
            check_text(s)
    with pytest.raises(TextError, match="index 1"):
        check_text("a\x00b\x01")
    with pytest.raises(TextError, match="index 4"):
        check_text("abc\u00e9e\u0301")  # NFD tail: first difference is where the decomposed pair starts
    with pytest.raises(TextError, match="index 1"):
        check_text("a\nb\nc", one_line=True)


def test_is_clean_text_honours_one_line():
    assert is_clean_text("a\nb") and not is_clean_text("a\nb", one_line=True)


def test_show_invisible_cannot_be_forged():
    real = "\u2060"
    forged = "\u27e8U+2060\u27e9"
    assert show_invisible(real) == forged
    assert show_invisible(forged) != show_invisible(real)
    assert show_invisible(forged) == "\u27e8U+27E8\u27e9U+2060\u27e9"
    # every marker opener in the output is a real marker
    out = show_invisible("x\u27e8y\u200bz\u27e8")
    assert out.count("\u27e8") == 3 == out.count("\u27e9")


def test_named_non_cf_lookalikes_are_shown():
    named = [0x034F, 0x115F, 0x1160, 0x2028, 0x2029, 0x3164, 0xFFA0, 0xFE00, 0xFE0F, 0xE0100, 0xE01EF]
    for cp in named:
        assert [(x.codepoint, x.kind) for x in suspicious("a" + chr(cp))] == [(cp, "invisible")], hex(cp)
    for cp in (0xFDFF, 0xFE10, 0xE00FF, 0xE01F0, 0x3000, 0x00A0):  # neighbours are not flagged
        assert suspicious(chr(cp)) == [], hex(cp)
    # the characters are accepted by the text rules (they are shown, not refused)
    for cp in named:
        assert check_text(chr(cp)) == chr(cp)


def test_typed_errors_for_non_str():
    for fn in (nfc, suspicious, show_invisible):
        for bad in (None, b"x", 1):
            with pytest.raises(TextError):
                fn(bad)  # type: ignore[arg-type]


def test_suspect_is_frozen_and_hashable():
    s = suspicious("a\u200b")[0]
    assert s == suspicious("a\u200b")[0] and hash(s) == hash(suspicious("a\u200b")[0])
    with pytest.raises(AttributeError):
        s.index = 3  # type: ignore[misc]


def test_source_files_hold_no_raw_invisible_or_nfd_text():
    # the test sources spell such characters as escapes, so a formatter or NFC hook cannot change what they test
    import pathlib

    for p in pathlib.Path(__file__).parent.glob("*.py"):
        t = p.read_text(encoding="utf-8")
        assert not suspicious(t.replace("\u00a7", "")), p.name
        assert all(ord(c) < 128 or c == "\u00a7" for c in t), p.name
