"""Hidden characters are decided by Unicode category, not a fixed list."""

import pytest

from orch_dashboard.text import clean, hidden


@pytest.mark.parametrize(
    "char",
    ["\u202e", "\u2066", "\u200b", "\u200f", "\ufeff", "\u2028", "\u2029", "\u061c", "\u2060", "\u2064", "\u00ad",
     "\u180e", "\U000e0041", "\ufff9", "\u0085", "\x00", "\t", "\n", "\x7f", "\u3164", "\ufe0f"],
)  # fmt: skip
def test_hidden_characters_are_refused(char):
    assert hidden(f"a{char}b")
    with pytest.raises(ValueError):
        clean(f"a{char}b")


@pytest.mark.parametrize("text", ["Main (a1b2c3d4)", "Ünïcode Nämé", "日本語", "a b", "emoji \U0001f600"])
def test_ordinary_text_passes(text):
    assert clean(text) == text
