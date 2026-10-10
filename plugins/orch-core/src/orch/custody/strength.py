"""Passphrase strength for a newly created key (C7 and C8 security reviews, owner decision). No dependencies.

A new passphrase must have at least :data:`MIN_CHARS` Unicode scalars after NFC and an estimated entropy of at least
:data:`MIN_ENTROPY_BITS`. The estimate is the cheapest way to *write* the text as a guesser would, found by dynamic
programming over its characters:

* a common password or a word of the BIP-39 list (also with leet substitutions:
  ``p4ssw0rd``) costs 12 or 11 bits,
  whatever its length, so ``summer2024summer`` is two words and a number, not sixteen characters;
* an ascending or descending run (``abcd``, ``4321``) or a keyboard run (``qwerty``, ``asdf``) of four or
  more characters costs 5 bits in all; a repeated character or a repeated block costs 1 to 2 bits;
  a year (1900 to 2099) costs 7 bits;
* any other character costs ``log2`` of the character pool the whole text draws on (lower case, upper case, digits,
  other), a separator (space, ``-``, ``_``, ``.``, ``,``) 3 bits.

The generated passphrase of ``orch init`` (six distinct BIP-39 words, about 66 bits) is judged by the same rule.
The common-password list is vendored next to this file
(``common_passwords.txt``, the first 5000 entries of the SecLists 10k list, MIT licence).

This is an estimate against offline guessing of a stolen key file, not a proof.
"""

from __future__ import annotations

import functools
import math
import re
from pathlib import Path

from orch import canon

from .base import CustodyError

__all__ = ["MIN_CHARS", "MIN_ENTROPY_BITS", "check_strength", "entropy_bits", "generate_passphrase"]

MIN_CHARS = 14
MIN_ENTROPY_BITS = 60.0
PHRASE_WORDS = 6
_SEPARATORS = " -_.,"
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")
_LEET = str.maketrans(
    {"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "@": "a", "$": "s", "!": "i"}
)


@functools.cache
def _common() -> frozenset[str]:
    text = (Path(__file__).parent / "common_passwords.txt").read_text(encoding="utf-8")
    return frozenset(x for x in text.split("\n") if x and not x.startswith("#"))


@functools.cache
def _bip() -> frozenset[str]:
    from orch.identity._wordlist import WORDS  # imported late: orch.identity imports this package

    return frozenset(WORDS)


def generate_passphrase(words: int = PHRASE_WORDS) -> str:
    """``words`` distinct random words of the BIP-39 list from the OS CSPRNG, separated by spaces."""
    import secrets

    from orch.identity._wordlist import WORDS

    return " ".join(secrets.SystemRandom().sample(WORDS, words))


def _pool_bits(text: str) -> float:
    pool = 0
    pool += 26 if re.search(r"[a-z]", text) else 0
    pool += 26 if re.search(r"[A-Z]", text) else 0
    pool += 10 if re.search(r"[0-9]", text) else 0
    pool += 33 if re.search(r"[^a-zA-Z0-9]", text) else 0
    return math.log2(max(pool, 10))


def _runs(low: str, i: int):
    """Lengths (>= 4) of an ascending/descending or keyboard run starting at ``i``."""
    n = len(low)
    for step in (1, -1):
        j = i + 1
        while j < n and ord(low[j]) - ord(low[j - 1]) == step and low[j].isalnum():
            j += 1
        for ln in range(4, j - i + 1):
            yield ln
    for row in _ROWS:
        for r in (row, row[::-1]):
            k = r.find(low[i])
            if k < 0:
                continue
            j = 1
            while i + j < n and k + j < len(r) and low[i + j] == r[k + j]:
                j += 1
            for ln in range(4, j + 1):
                yield ln


def entropy_bits(text: str) -> float:
    s = canon.nfc(text)
    low = s.lower()
    leet = low.translate(_LEET)
    n = len(s)
    char = _pool_bits(s)
    words = _common() | _bip()
    inf = float("inf")
    best = [inf] * (n + 1)
    best[0] = 0.0

    def relax(j: int, cost: float) -> None:
        if cost < best[j]:
            best[j] = cost

    for i in range(n):
        base = best[i]
        if base == inf:
            continue
        c = s[i]
        relax(i + 1, base + (3.0 if c in _SEPARATORS else char))
        if i > 0 and s[i - 1] == c:
            relax(i + 1, base + 1.0)  # a repeated character
        for ln in {k for k in _runs(low, i)}:
            relax(i + ln, base + 5.0)
        for b in range(2, min(i, n - i) + 1):  # a repeated block
            if s[i : i + b] == s[i - b : i]:
                relax(i + b, base + 2.0)
        if re.fullmatch(r"(?:19|20)[0-9]{2}", s[i : i + 4]):
            relax(i + 4, base + 7.0)
        for ln in range(3, min(n - i, 24) + 1):
            if low[i : i + ln] in words:
                relax(i + ln, base + (11.0 if low[i : i + ln] in _bip() else 12.0))
            elif leet[i : i + ln] in words:
                relax(i + ln, base + 14.0)
    return best[n]


def check_strength(passphrase: str) -> None:
    """Raise :class:`CustodyError` unless ``passphrase`` meets the rules in the module docstring."""
    text = canon.nfc(passphrase)
    if len(text) < MIN_CHARS:
        raise CustodyError(f"passphrase needs at least {MIN_CHARS} characters")
    folded = re.sub(r"[\s\-_.,]", "", text).lower().translate(_LEET)
    if folded in _common():
        raise CustodyError("that passphrase is on the list of common ones")
    if entropy_bits(text) < MIN_ENTROPY_BITS:
        raise CustodyError(
            "passphrase is too predictable (common words, runs, repeats or too short): "
            "use the generated one, or a long mix of unrelated words, digits and symbols"
        )
