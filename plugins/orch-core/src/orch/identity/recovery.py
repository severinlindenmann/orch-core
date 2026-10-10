"""The 24-word recovery code and the person key derived from it (D50; protocol §6.5 defers the format to P1).

**Wordlist and checksum (decision; the docs say only "24-word code").** BIP-39 English (2048 words, vendored in
``_wordlist.py`` with its SHA-256 pinned by a test): 24 words = 256 bits of entropy plus the BIP-39 8-bit checksum
(first byte of SHA-256 of the entropy). A mistyped word or a swap is caught by the checksum before any key is derived.

**Normalisation.** NFKD, lower case, split on whitespace, rejoined with single spaces. Only listed words are accepted
(so the normalised text is the same for everyone who types the same words).

**Derivation (decision; D50: "memory-hard KDF over the normalised code, then FIPS 186-5 A.2.1").**
``okm = scrypt(password = normalised code in UTF-8, salt = "orch/v2/person-key|" || suite byte, N = 2^17, r = 8,
p = 1, dklen = 40)`` and ``d = (int(okm) mod (n - 1)) + 1`` with ``n`` the P-256 group order, which is FIPS 186-5
A.2.1 (extra random bits: 320 bits for a 256-bit order, so the bias is below 2^-64) and never the vectors' seed mapping.
The 256-bit entropy of the code makes the KDF cost a second line of defence, not the only one. The parameters are
pinned by a known-answer test; changing them changes every person key, so they are part of the format.
"""

from __future__ import annotations

import secrets
import unicodedata

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from orch import crypto

from ._wordlist import WORDS

__all__ = [
    "PERSON_KEY_SALT",
    "RECOVERY_KDF",
    "RecoveryCodeError",
    "check_recovery_code",
    "code_from_entropy",
    "derive_person_key",
    "derive_person_scalar",
    "generate_recovery_code",
    "normalise_code",
]

PERSON_KEY_SALT = b"orch/v2/person-key|" + bytes([crypto.SUITE_ID])
RECOVERY_KDF = {"n": 2**17, "r": 8, "p": 1, "dklen": 40}
_INDEX = {w: i for i, w in enumerate(WORDS)}
assert len(WORDS) == 2048 and len(_INDEX) == 2048


class RecoveryCodeError(ValueError):
    """The code is not 24 valid words with a correct checksum. ``code`` is ``recovery.words``, ``recovery.unknown_word``
    or ``recovery.checksum``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def code_from_entropy(entropy: bytes) -> str:
    """The 24-word code of 32 bytes of entropy (BIP-39)."""
    if type(entropy) is not bytes or len(entropy) != 32:
        raise RecoveryCodeError("recovery.words", "entropy must be 32 bytes")
    bits = int.from_bytes(entropy + crypto.sha256(entropy)[:1], "big")
    return " ".join(WORDS[(bits >> (11 * (23 - i))) & 0x7FF] for i in range(24))


def generate_recovery_code() -> str:
    """A fresh code from the OS CSPRNG, shown once at person creation."""
    return code_from_entropy(secrets.token_bytes(32))


def normalise_code(code: str) -> str:
    """NFKD, lower case, single spaces. Does not validate."""
    if type(code) is not str:
        raise RecoveryCodeError("recovery.words", "not text")
    return " ".join(unicodedata.normalize("NFKD", code).lower().split())


def check_recovery_code(code: str) -> str:
    """Return the normalised code, or raise :class:`RecoveryCodeError` (wrong word count, unknown word, bad
    checksum)."""
    text = normalise_code(code)
    words = text.split(" ") if text else []
    if len(words) != 24:
        raise RecoveryCodeError("recovery.words", f"expected 24 words, got {len(words)}")
    try:
        idx = [_INDEX[w] for w in words]
    except KeyError:
        raise RecoveryCodeError("recovery.unknown_word") from None
    bits = 0
    for i in idx:
        bits = (bits << 11) | i
    entropy, checksum = (bits >> 8).to_bytes(32, "big"), bits & 0xFF
    if crypto.sha256(entropy)[0] != checksum:
        raise RecoveryCodeError("recovery.checksum")
    return text


def derive_person_scalar(code: str, *, _kdf_n: int = RECOVERY_KDF["n"]) -> int:
    """The P-256 scalar of a code. ``_kdf_n`` is a test seam (fast structural tests); production always uses 2^17."""
    text = check_recovery_code(code)
    okm = Scrypt(
        salt=PERSON_KEY_SALT, length=RECOVERY_KDF["dklen"], n=_kdf_n, r=RECOVERY_KDF["r"], p=RECOVERY_KDF["p"]
    ).derive(text.encode("utf-8"))
    return int.from_bytes(okm, "big") % (crypto.P256_N - 1) + 1


def derive_person_key(code: str, *, _kdf_n: int = RECOVERY_KDF["n"]) -> EllipticCurvePrivateKey:
    """The person key ``PK`` of a recovery code. Same code, same key, same ``person_id``."""
    return crypto.private_key_from_scalar(derive_person_scalar(code, _kdf_n=_kdf_n))
