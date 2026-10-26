"""Suite 2 primitives: round trips and the negative cases of protocol v2 §1.2 and §5."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from orch import crypto
from orch.crypto import sealing, suite
from orch.crypto.encoding import EncodingError

from .vectors import pub

N = crypto.P256_N


@pytest.fixture(scope="module")
def key():
    return crypto.generate_private_key()


def test_sign_verify_round_trip(key):
    sig = crypto.sign(key, b"msg")
    assert len(sig) == 64
    assert crypto.verify(crypto.public_bytes(key), sig, b"msg")
    assert not crypto.verify(crypto.public_bytes(key), sig, b"msh")


def test_public_key_is_65_byte_uncompressed(key):
    p = crypto.public_bytes(key)
    assert len(p) == 65 and p[0] == 4
    assert crypto.validate_public_key(p) == p


def test_signatures_are_randomised(key):
    assert crypto.sign(key, b"m") != crypto.sign(key, b"m")


@pytest.mark.parametrize("length", [0, 1, 63, 65, 96])
def test_signature_must_be_exactly_64_bytes(key, length):
    good = crypto.sign(key, b"m")
    bad = (good + bytes(40))[:length]
    assert not crypto.verify(crypto.public_bytes(key), bad, b"m")


@pytest.mark.parametrize(
    ("r", "s"),
    [(0, 1), (1, 0), (N, 1), (1, N), (N + 1, 1), (2**256 - 1, 2**256 - 1), (0, 0)],
)
def test_out_of_range_r_or_s_is_refused(key, r, s):
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert not crypto.verify(crypto.public_bytes(key), sig, b"m")


def test_high_s_twin_is_accepted(key):
    sig = crypto.sign(key, b"m")
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    twin = r.to_bytes(32, "big") + (N - s).to_bytes(32, "big")
    assert crypto.verify(crypto.public_bytes(key), twin, b"m")


def test_signature_under_another_key_fails(key):
    other = crypto.generate_private_key()
    assert not crypto.verify(crypto.public_bytes(other), crypto.sign(key, b"m"), b"m")


def test_verify_never_raises_on_garbage(key):
    p = crypto.public_bytes(key)
    for sig, msg in [(None, b"m"), ("x" * 64, b"m"), (b"\0" * 64, None)]:
        assert crypto.verify(p, sig, msg) is False
    assert crypto.verify(b"junk", b"\1" * 64, b"m") is False


def _compressed(p: bytes) -> bytes:
    return bytes([2 + (p[64] & 1)]) + p[1:33]


def test_invalid_public_keys_are_refused(key):
    p = crypto.public_bytes(key)
    off_curve = p[:64] + bytes([p[64] ^ 1])
    hybrid = bytes([6 + (p[64] & 1)]) + p[1:]
    identity = bytes(65)
    bad = {
        "off_curve": off_curve,
        "compressed": _compressed(p),
        "hybrid": hybrid,
        "identity": identity,
        "identity_tag4": b"\x04" + bytes(64),
        "short": p[:64],
        "long": p + b"\0",
        "empty": b"",
        "x_ge_p": b"\x04" + b"\xff" * 64,
    }
    for name, b in bad.items():
        with pytest.raises(crypto.CryptoError):
            crypto.validate_public_key(b)
        assert not crypto.verify(b, crypto.sign(key, b"m"), b"m"), name
    with pytest.raises(crypto.CryptoError):
        crypto.validate_public_key("not bytes")  # type: ignore[arg-type]


def test_vector_off_curve_point_is_refused():
    with pytest.raises(crypto.CryptoError):
        crypto.validate_public_key(bytes.fromhex(_vector("point_not_on_curve")["pub"]))


def _vector(name):
    from .vectors import S2

    return next(c for c in S2["sign"] if c["name"] == name)


def test_ecdh_is_symmetric_and_refuses_bad_peer(key):
    other = crypto.generate_private_key()
    a = crypto.ecdh(key, crypto.public_bytes(other))
    assert a == crypto.ecdh(other, crypto.public_bytes(key)) and len(a) == 32
    with pytest.raises(crypto.CryptoError):
        crypto.ecdh(key, bytes(65))


def test_scalar_range_is_enforced():
    for d in (0, N, N + 1, -1):
        with pytest.raises(crypto.CryptoError):
            crypto.private_key_from_scalar(d)
    k = crypto.private_key_from_scalar(1)
    assert crypto.private_scalar(k) == (1).to_bytes(32, "big")


def test_aes_gcm_round_trip_and_tamper():
    k = bytes(32)
    ct = crypto.aes_gcm_seal(k, bytes(12), b"pt", b"aad")
    assert crypto.aes_gcm_open(k, bytes(12), ct, b"aad") == b"pt"
    with pytest.raises(InvalidTag):
        crypto.aes_gcm_open(k, bytes(12), ct, b"aae")
    with pytest.raises(InvalidTag):
        crypto.aes_gcm_open(k, bytes(12), ct[:-1] + bytes([ct[-1] ^ 1]), b"aad")


def test_salted_aead_fresh_salt_and_negatives():
    k = bytes(range(32))
    a = crypto.salted_seal(k, b"orch/v2/push-msg", b"aad", b"hello")
    b = crypto.salted_seal(k, b"orch/v2/push-msg", b"aad", b"hello")
    assert a != b and a[:16] != b[:16]
    assert crypto.salted_open(k, b"orch/v2/push-msg", b"aad", a) == b"hello"
    for bad in (
        (k, b"orch/v2/wrap-msg", b"aad", a),  # wrong label
        (k, b"orch/v2/push-msg", b"other", a),  # wrong aad
        (bytes(32), b"orch/v2/push-msg", b"aad", a),  # wrong key
        (k, b"orch/v2/push-msg", b"aad", a[:20]),  # short
    ):
        with pytest.raises(InvalidTag):
            crypto.salted_open(*bad)


def test_hkdf_empty_salt_is_zero_salt():
    assert crypto.hkdf(b"ikm", b"", b"info") == crypto.hkdf(b"ikm", bytes(32), b"info")


# --- sealing ----------------------------------------------------------------------------------------------------------


def _seal_args():
    rcpt = crypto.generate_private_key()
    return rcpt, bytes(range(16)), bytes(range(16, 32))


def test_seal_round_trip_and_fresh_ephemeral_each_time():
    rcpt, rid, oid = _seal_args()
    p = crypto.public_bytes(rcpt)
    a = crypto.seal(p, rid, "wk", oid, 1, b"secret")
    b = crypto.seal(p, rid, "wk", oid, 1, b"secret")
    assert a[:65] != b[:65], "ephemeral key must be fresh for every seal"
    assert a != b
    assert crypto.open_sealed(rcpt, rid, "wk", oid, 1, a) == b"secret"
    assert crypto.open_sealed(rcpt, rid, "wk", oid, 1, b) == b"secret"


def test_seal_has_no_ephemeral_parameter():
    import inspect

    for fn in (crypto.seal, sealing._seal_with_ephemeral):
        names = set(inspect.signature(fn).parameters)
        if fn is crypto.seal:
            assert not names & {"eph", "eph_seed", "ephemeral", "eph_priv"}


def test_seal_draws_ephemeral_from_generate_private_key(monkeypatch):
    rcpt, rid, oid = _seal_args()
    calls = []
    real = suite.generate_private_key

    def spy():
        calls.append(1)
        return real()

    monkeypatch.setattr(suite, "generate_private_key", spy)
    crypto.seal(crypto.public_bytes(rcpt), rid, "sk", oid, 0, b"x")
    crypto.seal(crypto.public_bytes(rcpt), rid, "sk", oid, 0, b"x")
    assert len(calls) == 2


def test_seal_open_negatives():
    rcpt, rid, oid = _seal_args()
    p = crypto.public_bytes(rcpt)
    blob = crypto.seal(p, rid, "wk", oid, 7, b"secret", b"extra")
    other = crypto.generate_private_key()
    cases = [
        (rcpt, rid, "sk", oid, 7, blob, b"extra"),
        (rcpt, bytes(16), "wk", oid, 7, blob, b"extra"),
        (rcpt, rid, "wk", bytes(16), 7, blob, b"extra"),
        (rcpt, rid, "wk", oid, 8, blob, b"extra"),
        (rcpt, rid, "wk", oid, 7, blob, b""),
        (other, rid, "wk", oid, 7, blob, b"extra"),
        (rcpt, rid, "wk", oid, 7, blob[:-1] + bytes([blob[-1] ^ 1]), b"extra"),
        (rcpt, rid, "wk", oid, 7, blob[:70], b"extra"),
        (rcpt, rid, "wk", oid, 7, bytes(65) + blob[65:], b"extra"),  # identity ephemeral key
        (rcpt, rid, "wk", oid, 7, b"\x04" + bytes(64) + blob[65:], b"extra"),  # off-curve ephemeral key
    ]
    for c in cases:
        with pytest.raises(InvalidTag):
            crypto.open_sealed(*c)
    assert crypto.open_sealed(rcpt, rid, "wk", oid, 7, blob, b"extra") == b"secret"


def test_seal_refuses_unknown_purpose_and_bad_ids_and_bad_recipient_key():
    rcpt, rid, oid = _seal_args()
    p = crypto.public_bytes(rcpt)
    with pytest.raises(crypto.CryptoError):
        crypto.seal(p, rid, "nope", oid, 1, b"x")
    with pytest.raises(crypto.CryptoError):
        crypto.seal(p, rid[:15], "wk", oid, 1, b"x")
    with pytest.raises(crypto.CryptoError):
        crypto.seal(p, rid, "wk", oid, 2**32, b"x")
    with pytest.raises(crypto.CryptoError):
        crypto.seal(bytes(65), rid, "wk", oid, 1, b"x")


def test_id_derivation_is_domain_separated():
    p = pub("phone.sig")
    assert crypto.person_id(p) != crypto.device_id(p)
    assert len(crypto.person_id(p)) == 16
    assert crypto.pk_pin(p) != crypto.wsk_pin(p) and len(crypto.pk_pin(p)) == 32
    with pytest.raises(crypto.CryptoError):
        crypto.person_id(bytes(65))


def test_encoding_strictness():
    assert crypto.b64u(b"\x00\x01") == "AAE"
    assert crypto.unhex("00ff", 2) == b"\x00\xff"
    for bad in ("00FF", "00f", "00ffaa", 5):
        with pytest.raises(EncodingError):
            crypto.unhex(bad, 2)
    with pytest.raises(EncodingError):
        crypto.unb64u("AAE", 3)
