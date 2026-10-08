"""Text rules of ticket-format §3: NFC and LF, and nothing else."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.canon import is_normalized, normalize_text


def test_nfd_to_nfc():
    assert normalize_text("é") == "é"
    assert normalize_text("Å") == "Å"
    assert normalize_text("Å") == "Å"  # ANGSTROM SIGN composes to A-ring


def test_crlf_and_cr_become_lf():
    assert normalize_text("a\r\nb\rc\nd") == "a\nb\nc\nd"
    assert normalize_text("a\r\n\r\nb") == "a\n\nb"
    assert normalize_text("\r\r\n") == "\n\n"


@pytest.mark.parametrize(
    "s",
    ["trailing  \nspaces\t\n", "no final newline", "\n\nleading blank", "final\n\n\n", "  indented\n", "tab\there", ""],
)
def test_nothing_else_is_touched(s):
    assert normalize_text(s) == s


def test_other_unicode_untouched():
    # compatibility forms are NOT folded (that would be NFKC), and line/paragraph separators are not newlines
    assert normalize_text("ﬁ     ​") == "ﬁ     ​"


def test_is_normalized():
    assert is_normalized("abc\n")
    assert not is_normalized("a\r\n")
    assert not is_normalized("é")


def test_rejects_non_str():
    with pytest.raises(TypeError):
        normalize_text(b"abc")  # type: ignore[arg-type]


@settings(max_examples=200, deadline=None)
@given(st.text(st.characters(codec="utf-8"), max_size=60) | st.text(alphabet="é\r\néa", max_size=30))
def test_idempotent(s):
    n = normalize_text(s)
    assert normalize_text(n) == n
    assert is_normalized(n)
    assert "\r" not in n
