"""The 24 backup words (BIP-39 English, 256 bits + the 8-bit checksum, as core's recovery code). Generated here, shown once,
never stored: the hub keeps only ``bk_pub`` and ``K_auth`` (``contract.backup.derive_keys``)."""

from __future__ import annotations

import hashlib
import secrets

from ..contract.backup import normalise_words
from ._wordlist import WORDS

_INDEX = {w: i for i, w in enumerate(WORDS)}
assert len(WORDS) == 2048 and len(_INDEX) == 2048


class WordsError(ValueError):
    """The words are not 24 words of the list with a correct checksum; ``code`` is ``words``, ``unknown_word`` or ``checksum``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def words_from_entropy(entropy: bytes) -> str:
    if type(entropy) is not bytes or len(entropy) != 32:
        raise WordsError("words")
    bits = int.from_bytes(entropy + hashlib.sha256(entropy).digest()[:1], "big")
    return " ".join(WORDS[(bits >> (11 * (23 - i))) & 0x7FF] for i in range(24))


def generate_words() -> str:
    """24 fresh words from the OS random source."""
    return words_from_entropy(secrets.token_bytes(32))


def check_words(words: str) -> str:
    """The normalised words, or :class:`WordsError` (count, unknown word, checksum)."""
    text = normalise_words(words) if type(words) is str else ""
    parts = text.split(" ") if text else []
    if len(parts) != 24:
        raise WordsError("words")
    try:
        idx = [_INDEX[w] for w in parts]
    except KeyError:
        raise WordsError("unknown_word") from None
    bits = 0
    for i in idx:
        bits = (bits << 11) | i
    if hashlib.sha256((bits >> 8).to_bytes(32, "big")).digest()[0] != bits & 0xFF:
        raise WordsError("checksum")
    return text
