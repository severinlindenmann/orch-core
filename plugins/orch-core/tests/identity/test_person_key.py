"""Person key from the 24-word recovery code (D50): determinism, a pinned known answer, checksum refusal."""

from __future__ import annotations

import hashlib

import pytest

from orch import crypto
from orch.identity import (
    RecoveryCodeError,
    check_recovery_code,
    code_from_entropy,
    derive_person_key,
    generate_recovery_code,
    normalise_code,
)
from orch.identity import recovery as rec
from orch.identity._wordlist import WORDS

ZERO_CODE = " ".join(["abandon"] * 23 + ["art"])  # BIP-39 vector: 32 zero bytes
FAST = 2**10
# Pinned known answers (scrypt N, r=8, p=1, salt "orch/v2/person-key|\x02", dklen 40, A.2.1 reduction)
KAT_PRODUCTION_SCALAR = 0xDC823DA02B6E16F379EFD5C4A1CFBF9DD46884BD06D3C185046399DF4F2C8DCE
KAT_PRODUCTION_PUB = (
    "046fdc323a61ab1fe39c6294a3d45b2b027fb2b0dbcb630a8d3dd85566b084416c"
    "b1b0fb305a5ad8a5255c784e9cfef85d83072ce4c076e25a09c62b7c8dae8e20"
)
KAT_FAST_SCALAR = 0x599953D567FAA4A1F612490CE90871B9B00779CE57569775F3C6825707E4F7EB


def _independent(code: str, n: int) -> int:
    """The derivation re-done with hashlib.scrypt and plain integer arithmetic (no orch code)."""
    okm = hashlib.scrypt(code.encode(), salt=b"orch/v2/person-key|\x02", n=n, r=8, p=1, dklen=40, maxmem=2**28)
    order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    return int.from_bytes(okm, "big") % (order - 1) + 1


def test_wordlist_is_bip39_english():
    assert len(WORDS) == 2048 and len(set(WORDS)) == 2048
    assert hashlib.sha256(("\n".join(WORDS) + "\n").encode()).hexdigest() == (
        "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
    )
    assert WORDS[0] == "abandon" and WORDS[-1] == "zoo"


def test_bip39_known_codes():
    assert code_from_entropy(bytes(32)) == ZERO_CODE
    assert code_from_entropy(b"\xff" * 32) == " ".join(["zoo"] * 23 + ["vote"])
    assert check_recovery_code(ZERO_CODE) == ZERO_CODE


def test_generated_codes_are_valid_distinct_and_24_words():
    a, b = generate_recovery_code(), generate_recovery_code()
    assert a != b
    for c in (a, b):
        assert len(c.split()) == 24 and check_recovery_code(c) == c


def test_normalisation_is_case_and_space_insensitive():
    messy = "  " + ZERO_CODE.upper().replace(" ", "\n ", 5) + "\t"
    assert normalise_code(messy) == ZERO_CODE
    assert check_recovery_code(messy) == ZERO_CODE


def test_same_code_same_key_different_code_different_key():
    other = generate_recovery_code()
    a1 = derive_person_key(ZERO_CODE, _kdf_n=FAST)
    a2 = derive_person_key(normalise_code(ZERO_CODE.upper()), _kdf_n=FAST)
    b = derive_person_key(other, _kdf_n=FAST)
    assert crypto.public_bytes(a1) == crypto.public_bytes(a2)
    assert crypto.person_id(crypto.public_bytes(a1)) == crypto.person_id(crypto.public_bytes(a2))
    assert crypto.public_bytes(a1) != crypto.public_bytes(b)


def test_fast_known_answer_and_independent_derivation():
    assert rec.derive_person_scalar(ZERO_CODE, _kdf_n=FAST) == KAT_FAST_SCALAR == _independent(ZERO_CODE, FAST)
    pub = crypto.public_bytes(derive_person_key(ZERO_CODE, _kdf_n=FAST))
    assert crypto.person_id(pub).hex() == "013a2382f5403a0ff95a70870dd9a862"


def test_production_known_answer():
    """The pinned person key of the all-zero code at N = 2^17: changing any KDF parameter breaks every person key."""
    assert rec.RECOVERY_KDF == {"n": 2**17, "r": 8, "p": 1, "dklen": 40}
    key = derive_person_key(ZERO_CODE)
    assert (
        crypto.private_scalar(key)
        == KAT_PRODUCTION_SCALAR.to_bytes(32, "big")
        == (_independent(ZERO_CODE, 2**17).to_bytes(32, "big"))
    )
    assert crypto.public_bytes(key).hex() == KAT_PRODUCTION_PUB


def test_scalar_is_in_range_for_extreme_kdf_output(monkeypatch):
    class Fake:
        def __init__(self, **kw):
            pass

        def derive(self, data):
            return b"\xff" * 40

    monkeypatch.setattr(rec, "Scrypt", Fake)
    d = rec.derive_person_scalar(ZERO_CODE)
    assert 1 <= d <= crypto.P256_N - 1
    monkeypatch.setattr(Fake, "derive", lambda self, data: bytes(40))
    assert rec.derive_person_scalar(ZERO_CODE) == 1


def test_bad_checksum_is_refused_and_derives_nothing(monkeypatch):
    bad = " ".join(["abandon"] * 24)  # last word wrong: checksum fails
    monkeypatch.setattr(rec, "Scrypt", lambda **kw: pytest.fail("KDF must not run for an invalid code"))
    with pytest.raises(RecoveryCodeError) as e:
        derive_person_key(bad)
    assert e.value.code == "recovery.checksum"
    # a swapped pair of words of a valid code is also caught
    words = ZERO_CODE.split()
    words[0], words[-1] = words[-1], words[0]
    with pytest.raises(RecoveryCodeError) as e:
        check_recovery_code(" ".join(words))
    assert e.value.code == "recovery.checksum"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("", "recovery.words"),
        ("abandon " * 12, "recovery.words"),
        (" ".join(["abandon"] * 25), "recovery.words"),
        (" ".join(["abandon"] * 23 + ["notaword"]), "recovery.unknown_word"),
        (" ".join(["abandon"] * 23 + ["arts"]), "recovery.unknown_word"),
    ],
)
def test_malformed_codes_are_refused(code, reason):
    with pytest.raises(RecoveryCodeError) as e:
        check_recovery_code(code)
    assert e.value.code == reason


def test_non_text_is_refused():
    with pytest.raises(RecoveryCodeError):
        check_recovery_code(None)  # type: ignore[arg-type]
    with pytest.raises(RecoveryCodeError):
        code_from_entropy(b"short")
