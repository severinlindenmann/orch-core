"""Text forms of bytes (orch-relay protocol §2.2): lower-case hex for ids, canonical unpadded base64url for the rest."""

from __future__ import annotations

import base64
import re

__all__ = ["EncodingError", "b64u", "hex_id", "unb64u", "unhex"]


class EncodingError(ValueError):
    """A byte string is not in its one canonical text form."""


_B64U = re.compile(r"[A-Za-z0-9_-]*")
_HEX = re.compile(r"(?:[0-9a-f]{2})*")


def b64u(data: bytes) -> str:
    """Canonical base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(s: object, n: int | None = None) -> bytes:
    """Decode canonical unpadded base64url; refuse padding, the standard alphabet, length 4k+1 and non-zero trailing
    bits. ``n`` is the exact decoded length when the field has one."""
    if type(s) is not str or not _B64U.fullmatch(s) or len(s) % 4 == 1:
        raise EncodingError("not canonical base64url")
    data = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if b64u(data) != s:
        raise EncodingError("non-canonical base64url (trailing bits)")
    if n is not None and len(data) != n:
        raise EncodingError(f"expected {n} bytes, got {len(data)}")
    return data


def unhex(s: object, n: int) -> bytes:
    """Lower-case hex of exactly ``n`` bytes."""
    if type(s) is not str or len(s) != 2 * n or not _HEX.fullmatch(s):
        raise EncodingError(f"not lower-case hex of {n} bytes")
    return bytes.fromhex(s)


def hex_id(data: bytes) -> str:
    return data.hex()
