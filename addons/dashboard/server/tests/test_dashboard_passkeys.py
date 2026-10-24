"""WebAuthn verification (host spec §0.8.6 item 10.6, #1198 R1-2): the vectors, then adversarial checks."""

import hashlib
import json
from pathlib import Path

import pytest

from orch_dashboard import passkeys
from orch_dashboard.passkeys import PasskeyError, b64u_decode, b64u_encode

VECTORS = Path(__file__).parent / "vectors"


def load(name):
    return json.loads((VECTORS / name).read_text())


def d(text):
    return b64u_decode(text, 1 << 16)


def run_registration(v, case):
    return passkeys.verify_registration(
        credential_id=d(case["credential_id"]),
        client_data_json=d(case["client_data_json"]),
        attestation_object=d(case["attestation_object"]),
        challenge=d(case["challenge"]),
        rp_id=v["rp_id"],
        origin=v["origin"],
    )


def run_assertion(v, case, challenge=None):
    return passkeys.verify_assertion(
        credential_id=d(case["credential_id"]),
        client_data_json=d(case["client_data_json"]),
        authenticator_data=d(case["authenticator_data"]),
        signature=d(case["signature"]),
        public_key=d(case["public_key"]),
        challenge=d(case["challenge"]) if challenge is None else challenge,
        rp_id=v["rp_id"],
        origin=v["origin"],
    )


REG = load("webauthn_registration.json")
ASR = load("webauthn_assertion.json")
STEP = load("webauthn_stepup.json")


@pytest.mark.parametrize("case", REG["cases"], ids=lambda c: c["name"])
def test_registration_vectors(case):
    if case["expect"] == "ok":
        r = run_registration(REG, case)
        assert r.public_key == d(case["public_key"])
        assert r.alg == case["alg"]
        assert r.credential_id == d(case["credential_id"])
        assert passkeys.parse_public_key(r.public_key)[0] == r.alg
    else:
        with pytest.raises(PasskeyError) as e:
            run_registration(REG, case)
        assert e.value.reason == case["expect"]
        assert e.value.code == "passkey.invalid"


@pytest.mark.parametrize("case", ASR["cases"], ids=lambda c: c["name"])
def test_assertion_vectors(case):
    if case["expect"] == "ok":
        assert run_assertion(ASR, case).sign_count == case["sign_count"]
    else:
        with pytest.raises(PasskeyError) as e:
            run_assertion(ASR, case)
        assert e.value.reason == case["expect"]


def test_vectors_cover_both_algorithms_and_many_failures():
    ok = [c for c in REG["cases"] if c["expect"] == "ok"]
    assert {c["alg"] for c in ok} == {-7, -8}
    assert len({c["expect"] for c in REG["cases"] + ASR["cases"]}) >= 30


def test_stepup_challenge_vectors():
    for c in STEP["challenges"]:
        assert passkeys.stepup_challenge(d(c["nonce"]), c["view_digest"]) == d(c["challenge"])
    a, b = STEP["challenges"][:2]
    assert a["challenge"] != b["challenge"]  # another view, another challenge
    for c in STEP["bad_inputs"]:
        with pytest.raises(PasskeyError) as e:
            passkeys.stepup_challenge(d(c["nonce"]), c["view_digest"])
        assert e.value.reason == c["expect"]


@pytest.mark.parametrize("case", STEP["assertions"], ids=lambda c: c["name"])
def test_stepup_assertion_bound_to_view(case):
    expected = passkeys.stepup_challenge(d(STEP["nonce"]), case["check_view"])
    if case["expect"] == "ok":
        run_assertion(STEP, case, challenge=expected)
    else:
        with pytest.raises(PasskeyError) as e:
            run_assertion(STEP, case, challenge=expected)
        assert e.value.reason == case["expect"]


def test_stepup_challenge_is_domain_separated():
    nonce, vd = bytes(32), "sha256:" + "00" * 32
    assert passkeys.stepup_challenge(nonce, vd) != hashlib.sha256(nonce + bytes(32)).digest()
    assert passkeys.stepup_challenge(nonce, vd)[:4] != nonce[:4]


# --- adversarial ----------------------------------------------------------------------------------------------------

GOOD_REG = next(c for c in REG["cases"] if c["name"] == "es256 none")
GOOD_ASR = next(c for c in ASR["cases"] if c["name"] == "es256")


@pytest.mark.parametrize(
    ("rp_id", "origin", "reason"),
    [
        ("orch.example.com", "https://orch.example.com:443", "origin_config"),
        ("orch.example.com", "http://orch.example.com", "origin_config"),
        ("ORCH.example.com", "https://ORCH.example.com", "rp_id"),
        ("localhost", "https://localhost", "rp_id"),
        ("203.0.113.5", "https://203.0.113.5", "rp_id"),
        ("orch.example.com.", "https://orch.example.com.", "rp_id"),
        ("-orch.example.com", "https://-orch.example.com", "rp_id"),
        ("orch..com", "https://orch..com", "rp_id"),
        ("orch.example.com:8443", "https://orch.example.com:8443", "rp_id"),
    ],
)
def test_rp_configuration_fails_closed(rp_id, origin, reason):
    with pytest.raises(PasskeyError) as e:
        run_assertion({"rp_id": rp_id, "origin": origin}, GOOD_ASR)
    assert e.value.reason == reason


def test_rp_id_of_the_vectors_does_not_verify_for_a_parent_name():
    with pytest.raises(PasskeyError) as e:
        run_assertion({"rp_id": "example.com", "origin": "https://example.com"}, GOOD_ASR)
    assert e.value.reason == "origin"


def test_expected_challenge_must_be_32_bytes():
    for bad in (b"", bytes(31), bytes(33), "x" * 32):
        with pytest.raises(PasskeyError):
            run_assertion(ASR, GOOD_ASR, challenge=bad)


def test_signature_does_not_transfer_between_cases():
    eddsa = next(c for c in ASR["cases"] if c["name"] == "eddsa")
    with pytest.raises(PasskeyError):
        run_assertion(ASR, {**GOOD_ASR, "signature": eddsa["signature"]})
    with pytest.raises(PasskeyError):
        run_assertion(ASR, {**eddsa, "public_key": GOOD_ASR["public_key"]})


def test_client_data_bytes_are_what_is_signed():
    # The same JSON with a space inserted: still valid and equal as an object, but not the signed bytes.
    cd = d(GOOD_ASR["client_data_json"]).replace(b",", b", ", 1)
    with pytest.raises(PasskeyError) as e:
        run_assertion(ASR, {**GOOD_ASR, "client_data_json": b64u_encode(cd)})
    assert e.value.reason == "signature"


def test_every_single_byte_change_of_auth_data_is_refused():
    ad = d(GOOD_ASR["authenticator_data"])
    for i in range(len(ad)):
        for bit in (0x01, 0x80):
            changed = bytearray(ad)
            changed[i] ^= bit
            with pytest.raises(PasskeyError):
                run_assertion(ASR, {**GOOD_ASR, "authenticator_data": b64u_encode(bytes(changed))})


def test_size_limits():
    big = b64u_encode(b"{" + b" " * passkeys.MAX_CLIENT_DATA + b"}")
    with pytest.raises(PasskeyError) as e:
        run_assertion(ASR, {**GOOD_ASR, "client_data_json": big})
    assert e.value.reason == "client_data_size"
    with pytest.raises(PasskeyError) as e:
        run_assertion(ASR, {**GOOD_ASR, "signature": b64u_encode(bytes(129))})
    assert e.value.reason == "signature_size"
    with pytest.raises(PasskeyError) as e:
        run_assertion(ASR, {**GOOD_ASR, "credential_id": b64u_encode(bytes(1024))})
    assert e.value.reason == "credential_id"


@pytest.mark.parametrize(
    "text",
    ["AA==", "AA=", "A", "AB", "+/8", "a b", "ÄA", "AAA\n"],
)
def test_b64u_is_strict(text):
    with pytest.raises(PasskeyError):
        b64u_decode(text, 64)


def test_b64u_round_trip_and_limit():
    for n in range(0, 40):
        raw = bytes(range(n))
        assert b64u_decode(b64u_encode(raw), 64) == raw
    with pytest.raises(PasskeyError) as e:
        b64u_decode(b64u_encode(bytes(65)), 64)
    assert e.value.reason == "too_long"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "cbor_truncated"),
        (b"\x18", "cbor_truncated"),
        (b"\x17", None),
        (b"\x18\x17", "cbor_not_shortest"),
        (b"\x19\x00\xff", "cbor_not_shortest"),
        (b"\x1a\x00\x00\xff\xff", "cbor_not_shortest"),
        (b"\x1b\x00\x00\x00\x00\xff\xff\xff\xff", "cbor_not_shortest"),
        (b"\x5f\x41\x00\xff", "cbor_indefinite"),
        (b"\x1c", "cbor_indefinite"),
        (b"\xf6", "cbor_simple"),
        (b"\xf7", "cbor_simple"),
        (b"\xfb" + bytes(8), "cbor_simple"),
        (b"\xc0\x00", "cbor_tag"),
        (b"\x62\xc3\x28", "cbor_utf8"),
        (b"\x5a\xff\xff\xff\xff", "cbor_truncated"),
        (b"\x9b\x00\x00\x00\x01\x00\x00\x00\x00", "cbor_truncated"),
        (b"\xa1\x41\x00\x00", "cbor_key_type"),
        (b"\xa1\xf5\x00", "cbor_key_type"),
        (b"\xa2\x01\x00\x01\x00", "cbor_duplicate_key"),
        (b"\x81" * 9 + b"\x00", "cbor_depth"),
        (b"\x81" * 7 + b"\x00", None),
        (b"\x00\x00", "cbor_trailing"),
    ],
)
def test_cbor_subset(data, reason):
    if reason is None:
        passkeys.cbor_decode(data)
        return
    with pytest.raises(PasskeyError) as e:
        passkeys.cbor_decode(data)
    assert e.value.reason == reason


def test_cbor_fuzz_never_raises_anything_else():
    import random

    rnd = random.Random(1198)
    seeds = [d(c["attestation_object"]) for c in REG["cases"][:5]]
    for _ in range(3000):
        data = bytearray(rnd.choice(seeds))
        for _ in range(rnd.randint(1, 4)):
            data[rnd.randrange(len(data))] = rnd.randrange(256)
        if rnd.random() < 0.3:
            data = data[: rnd.randrange(len(data) + 1)]
        try:
            run_registration(REG, {**GOOD_REG, "attestation_object": b64u_encode(bytes(data))})
        except PasskeyError:
            pass


# --- Ed25519 small-order blocklist, checked with independent point arithmetic ---------------------------------------

P = 2**255 - 19
D_ED = -121665 * pow(121666, P - 2, P) % P


def _ed_decode(raw):
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    sign = raw[31] >> 7
    y %= P  # the non-canonical spellings (y >= p) decode to the same point in lax decoders
    u, v = (y * y - 1) % P, (D_ED * y * y + 1) % P
    x2 = u * pow(v, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * pow(2, (P - 1) // 4, P) % P
    if (x * x - x2) % P:
        return None
    if x & 1 != sign:
        x = (P - x) % P
    return x, y


def _ed_add(a, b):
    (x1, y1), (x2, y2) = a, b
    t = D_ED * x1 * x2 * y1 * y2 % P
    return (
        (x1 * y2 + x2 * y1) * pow(1 + t, P - 2, P) % P,
        (y1 * y2 + x1 * x2) * pow(1 - t, P - 2, P) % P,
    )


def test_blocklist_holds_only_small_order_points_and_all_of_them():
    for raw in passkeys.ED25519_SMALL_ORDER:
        pt = _ed_decode(raw)
        if pt is None:
            continue  # not even a point (x = 0 with the sign bit): refused anyway
        q = pt
        for _ in range(3):
            q = _ed_add(q, q)
        assert q == (0, 1), raw.hex()
    # Every point of order dividing 8, in canonical form, is on the list.
    canon = {r for r in passkeys.ED25519_SMALL_ORDER if int.from_bytes(r, "little") & ((1 << 255) - 1) < P}
    pts = {_ed_decode(r) for r in canon} - {None}
    assert len(pts) == 8


def test_registration_rejects_each_small_order_key():
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_webauthn_vectors", VECTORS / "make_webauthn_vectors.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    for raw in sorted(passkeys.ED25519_SMALL_ORDER):
        cose = gen.cbor({1: 1, 3: -8, -1: 6, -2: raw})
        att = gen.att_object(gen.auth_head(gen.UP | gen.UV | gen.AT) + gen.attested(gen.CRED_ES, cose))
        with pytest.raises(PasskeyError) as e:
            run_registration(REG, {**GOOD_REG, "attestation_object": b64u_encode(att)})
        assert e.value.reason == "cose_eddsa_small_order"
