"""Text normalisation for everything that is stored, signed or hashed.

Implements the "Text rules" of ``docs/architecture/orch-v2-ticket-format.md`` §3: **NFC** and **LF line
endings**. Nothing else is normalised: the format is silent on trailing whitespace, final newlines and
leading/trailing blank lines, so those are left exactly as written (see the PR's open questions).
"""

from __future__ import annotations

import unicodedata

__all__ = ["is_normalized", "normalize_text"]


def normalize_text(text: str) -> str:
    """CRLF and lone CR become LF, then the result is put in Unicode NFC. Idempotent."""
    if not isinstance(text, str):
        raise TypeError("normalize_text takes str")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text)


def is_normalized(text: str) -> bool:
    return "\r" not in text and unicodedata.is_normalized("NFC", text)
