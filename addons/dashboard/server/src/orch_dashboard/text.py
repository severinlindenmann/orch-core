"""Text that came from a host or a person and is shown or signed: no invisible, bidi or control characters.

Decided by Unicode category, not a fixed list: Cf (format: bidi marks and isolates, zero-width, soft hyphen, word
joiner, Arabic letter mark, tag characters), Cc (control), Zl and Zp (line and paragraph separators),
Co (private use), plus a few default-ignorable characters of other categories.
Refused, never repaired: what is shown is what was sent.
"""

import unicodedata

# Core's list (canon/text.py `_EXTRA_INVISIBLE`) plus the other default-ignorables the earlier rounds found.
EXTRA = {
    0x00AD,
    0x034F,
    0x061C,
    0x115F,
    0x1160,
    0x180E,
    0x2800,
    0x3164,
    0xFFA0,
    *range(0x17B4, 0x17B6),
    *range(0x180B, 0x1810),
    *range(0xFE00, 0xFE10),
    *range(0x2060, 0x2065),
    *range(0xE0000, 0xE0080),
    *range(0xE0100, 0xE01F0),
}
HIDDEN_CATEGORIES = ("Cf", "Cc", "Zl", "Zp", "Co")  # Co: private use (no glyph is defined for it)


def hidden(text: str) -> bool:
    return any(unicodedata.category(c) in HIDDEN_CATEGORIES or ord(c) in EXTRA for c in text)


def clean(text: str) -> str:
    """`text` itself, or ValueError when it holds a hidden character (pydantic turns that into a 400)."""
    if hidden(text):
        raise ValueError("the text has invisible or control characters")
    return text
