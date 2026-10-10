"""Text rules of ticket-format §11.3: NFC (Unicode 16.0), LF, refused controls, bidi and unassigned code points."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.canon import (
    HashError,
    TextError,
    check_text,
    is_normalized,
    normalize_text,
    section_hash,
    show_invisible,
    suspicious,
)

# anything the store might see: assigned text, controls, bidi, surrogates
_chars = st.characters(codec="utf-8", exclude_categories=())
_texts = st.text(_chars, max_size=40) | st.text(alphabet="ẹ́éa\r\n\t각 ", max_size=30)


def test_nfd_to_nfc_and_crs():
    assert normalize_text("é") == "é"
    assert normalize_text("Å") == "Å"
    assert normalize_text("a\r\nb\rc\nd") == "a\nb\nc\nd"


@pytest.mark.parametrize(
    "s",
    ["trailing  \nspaces\t\n", "no final newline", "\n\nleading blank", "final\n\n\n", "  indented\n", "tab\there", ""],
)
def test_nothing_else_is_touched(s):
    assert normalize_text(s) == s
    assert check_text(s) == s


def test_compat_forms_not_folded_and_ls_ps_are_not_newlines():
    s = "ﬁ     ​"
    assert normalize_text(s) == s


def test_unicode_16_is_pinned_not_the_runtime():
    # Garay (U+10D40) and Todhri/Tulu-Tigalari came with 16.0: accepted whatever the runtime says
    assert check_text("\U00010d40\U00011380") == "\U00010d40\U00011380"
    # U+0378 is unassigned in every version
    with pytest.raises(TextError):
        check_text("͸")


def test_controls_tab_and_lf_only():
    for ch in [chr(c) for c in range(0x20) if chr(c) not in "\n\t"] + ["\x7f", "\x80", "\x9f"]:
        with pytest.raises(TextError):
            check_text("a" + ch)
        with pytest.raises(TextError):
            check_text("é" + ch)  # non-ASCII path
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


def test_is_normalized():
    assert is_normalized("abc\n")
    assert not is_normalized("a\r\n")
    assert not is_normalized("é")
    assert not is_normalized("\x00")
    assert not is_normalized("\ud800")


def test_suspicious_and_show_invisible():
    s = "a​b﻿c\U000e0041­"
    got = suspicious(s)
    assert [(x.index, x.codepoint, x.kind) for x in got] == [
        (1, 0x200B, "invisible"),
        (3, 0xFEFF, "invisible"),
        (5, 0xE0041, "invisible"),
        (6, 0xAD, "invisible"),
    ]
    assert show_invisible(s) == "a⟨U+200B⟩b⟨U+FEFF⟩c⟨U+E0041⟩⟨U+00AD⟩"
    assert show_invisible("plain") == "plain"
    assert [x.kind for x in suspicious("x‮y؜")] == ["bidi", "bidi"]
    assert suspicious("clean text é") == []


@settings(max_examples=150, deadline=None)
@given(_texts)
def test_normalize_idempotent_and_result_is_valid(s):
    try:
        n = normalize_text(s)
    except TextError:
        return  # refused input stays refused; the next test checks it is refused for a stated reason
    assert normalize_text(n) == n
    assert check_text(n) == n
    assert is_normalized(n)
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
    assert is_normalized(s) == (n == s)
