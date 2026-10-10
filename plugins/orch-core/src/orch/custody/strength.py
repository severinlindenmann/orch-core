"""Passphrase strength for a newly created key (C7 security review): at least 12 Unicode scalars after NFC, not a
common passphrase, and a simple entropy estimate above a floor. No dependencies; a small built-in list."""

from __future__ import annotations

import math
import re

from orch import canon

from .base import CustodyError

__all__ = ["MIN_CHARS", "MIN_ENTROPY_BITS", "check_strength"]

MIN_CHARS = 12
MIN_ENTROPY_BITS = 40.0
_COMMON = frozenset(
    "passwordpassword password1234 123456789012 1234567890123 qwertyuiop12 qwertyuiopasdf letmeinletmein "
    "iloveyou1234 administrator changemechangeme welcome12345 correcthorsebatterystaple passphrase1234 "
    "trustno1trustno1 abcdefghijkl abcdefghijklmnop 111111111111 000000000000 aaaaaaaaaaaa".split()
)


def _entropy_bits(text: str) -> float:
    """Length times log2 of the character pool, capped by the distinct characters, minus a penalty for runs and
    ascending or repeating patterns. A rough floor, not a proof."""
    pool = 0
    for pat, size in ((r"[a-z]", 26), (r"[A-Z]", 26), (r"[0-9]", 10), (r"[^a-zA-Z0-9]", 32)):
        if re.search(pat, text):
            pool += size
    distinct = len(set(text))
    bits = min(len(text), distinct * 2) * math.log2(max(pool, 2))
    if re.search(r"(.{1,3})\1{2,}", text):  # abcabcabc, aaaa
        bits *= 0.5
    return bits


def check_strength(passphrase: str) -> None:
    text = canon.nfc(passphrase)
    if len(text) < MIN_CHARS:
        raise CustodyError(f"passphrase needs at least {MIN_CHARS} characters")
    if re.sub(r"[\s\-_]", "", text).lower() in _COMMON:
        raise CustodyError("that passphrase is on the list of common ones")
    if _entropy_bits(text) < MIN_ENTROPY_BITS:
        raise CustodyError("passphrase is too predictable: use a longer phrase of several unrelated words")
