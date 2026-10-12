"""Text that came from a host or a person and is shown or signed: no invisible, bidi or control characters.

Decided by Unicode category, not a fixed list: Cf (format: bidi marks and isolates, zero-width, soft hyphen, word
joiner, Arabic letter mark, tag characters), Cc (control), Zl and Zp (line and paragraph separators), plus a few
default-ignorable characters of other categories. Refused, never repaired: what is shown is what was sent.
"""

import unicodedata

EXTRA = {0x00AD, 0x061C, 0x180E, 0x3164, 0xFE0F, *range(0x2060, 0x2065), *range(0xE0000, 0xE0080)}


def hidden(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cf", "Cc", "Zl", "Zp") or ord(c) in EXTRA for c in text)


def clean(text: str) -> str:
    """`text` itself, or ValueError when it holds a hidden character (pydantic turns that into a 400)."""
    if hidden(text):
        raise ValueError("the text has invisible or control characters")
    return text
