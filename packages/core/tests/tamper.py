"""Tampering that always tampers. A fixed suffix such as ``sig[:-2] + "AA"`` changes nothing when a random value
already ends that way (or when the last base64 characters only carry padding bits), and the test then fails one run in
a few thousand. These helpers change a value for certain and assert it."""

from __future__ import annotations

import base64


def tamper_b64u(text: str, *, byte: int | None = None) -> str:
    """``text`` (unpadded base64url) with one bit flipped in a decoded byte (the middle one by default), re-encoded."""
    raw = bytearray(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    raw[len(raw) // 2 if byte is None else byte] ^= 0x01
    out = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    assert out != text and base64.urlsafe_b64decode(out + "=" * (-len(out) % 4)) != base64.urlsafe_b64decode(
        text + "=" * (-len(text) % 4)
    )
    return out


def tamper_text(text: str) -> str:
    """``text`` with the character in the middle replaced by a different one (for opaque tokens)."""
    i = len(text) // 2
    out = text[:i] + ("A" if text[i] != "A" else "B") + text[i + 1 :]
    assert out != text
    return out
