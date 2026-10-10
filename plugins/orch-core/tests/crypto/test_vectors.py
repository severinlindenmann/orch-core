"""Every suite-2 vector of ``vectors_v2.json`` that the crypto layer owns (protocol v2 §1, §3, §4, §5).

Sections of ``suites.2`` consumed elsewhere: ``certs``, ``revocations`` and ``card`` (delegation cases) in
``tests/identity/test_certs.py``; ``question_hash``, ``decisions``, ``webauthn`` and ``publish`` by canon/schema tests.

Sections **deferred**, with the phase that owns them (nothing in C2 builds those objects; they need the relay,
bridge or Drop code): ``card`` (card/sealed part, relay update rules), ``wk_grants`` and ``member_lists`` (relay
member lists, P2/P3), ``cert_request``, ``enroll`` and ``bridge`` (pairing and bridge v2, P2/P3), ``revocation_op``
(host revocation op, P2), ``push`` (P3), ``drop`` and ``ws_envelopes`` (Drop and ws-to-ws, P2+), ``relay_auth``
(relay login, P3). ``test_deferred_sections_are_complete`` fails when the vector file gains a section nobody has
listed, so a vector update cannot go unconsumed silently.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec

from orch import crypto
from orch.crypto import labels, sealing, suite
from orch.crypto.encoding import EncodingError

from .vectors import ALL, KEYS, S2, priv, pub


def test_vector_file_is_the_expected_version():
    assert ALL["version"] == 2
    assert S2["suite"] == 2 and S2["name"] == "p256"


def test_labels_match_the_vector_table():
    assert labels.LABELS == ALL["labels"]


def test_signature_aad_and_hash_labels_are_prefix_free():
    domains = [v for k, v in labels.LABELS.items() if not k.startswith(("kdf_", "mac_"))]
    for a in domains:
        for b in domains:
            assert a == b or not b.startswith(a), (a, b)
    assert len(set(labels.LABELS.values())) == len(labels.LABELS)


def test_event_labels_are_prefix_free_together_with_protocol_labels():
    from orch import canon

    every = list(labels.signature_labels()) + [v.encode() for v in canon.LABELS.values()]
    for a in set(every):
        for b in set(every):
            assert a == b or not b.startswith(a), (a, b)


@pytest.mark.parametrize("case", S2["sign"], ids=lambda c: c["name"])
def test_sign_vectors(case):
    got = crypto.verify(bytes.fromhex(case["pub"]), bytes.fromhex(case["sig"]), bytes.fromhex(case["msg"]))
    assert got is case["valid"]


def test_vector_public_keys_derive_from_seeds():
    for name, k in KEYS.items():
        assert crypto.public_bytes(priv(name)) == bytes.fromhex(k["pub"]), name


@pytest.mark.parametrize("case", S2["hkdf"], ids=lambda c: c["name"])
def test_hkdf_vectors(case):
    assert crypto.hkdf(bytes.fromhex(case["ikm"]), bytes.fromhex(case["salt"]), bytes.fromhex(case["info"])) == (
        bytes.fromhex(case["okm"])
    )
    assert bytes.fromhex(case["info"]).decode() == case["info_text"]


@pytest.mark.parametrize("case", S2["salted_aead"], ids=lambda c: c["name"])
def test_salted_aead_vectors(case):
    k, label, aad = bytes.fromhex(case["k_base"]), case["msg_label"].encode(), bytes.fromhex(case["aad"])
    salt, pt = bytes.fromhex(case["salt"]), bytes.fromhex(case["plaintext"])
    assert crypto.suite._salted_seal(k, label, aad, pt, salt) == bytes.fromhex(case["sealed"])
    assert crypto.salted_open(k, label, aad, bytes.fromhex(case["sealed"])) == pt
    assert crypto.hkdf(k, salt, label) == bytes.fromhex(case["k_msg"])


@pytest.mark.parametrize("case", S2["seal"], ids=lambda c: c["name"])
def test_seal_vectors(case):
    rid, oid = bytes.fromhex(case["recipient_id"]), bytes.fromhex(case["object_id"])
    eph = suite.private_key_from_scalar(int.from_bytes(bytes.fromhex(case["eph_seed"]), "big") % (suite.P256_N - 1) + 1)
    rcpt_pub = bytes.fromhex(case["recipient_kx_pub"])
    assert crypto.public_bytes(eph) == bytes.fromhex(case["eph_pub"])
    assert crypto.ecdh(eph, rcpt_pub) == bytes.fromhex(case["shared_secret"])
    sealed = sealing._seal_with_ephemeral(
        eph,
        rcpt_pub,
        rid,
        case["purpose"],
        oid,
        case["epoch"],
        bytes.fromhex(case["plaintext"]),
        bytes.fromhex(case["extra_aad"]),
    )
    assert sealed == bytes.fromhex(case["sealed"])
    assert sealing.seal_aad(case["purpose"], oid, case["epoch"]).hex() == case["aad"]


@pytest.mark.parametrize("case", S2["seal_open"], ids=lambda c: c["name"])
def test_seal_open_vectors(case):
    kx = priv(f"{case['recipient']}.kx")
    args = (
        kx,
        bytes.fromhex(case["recipient_id"]),
        case["purpose"],
        bytes.fromhex(case["object_id"]),
        case["epoch"],
        bytes.fromhex(case["sealed"]),
    )
    if case["plaintext"] is None:
        with pytest.raises(InvalidTag):
            crypto.open_sealed(*args)
    else:
        assert crypto.open_sealed(*args) == bytes.fromhex(case["plaintext"])


def test_label_sealed_vector():
    c = S2["label_sealed"]
    from orch.identity.certs import open_device_label, seal_device_label

    pvk = bytes.fromhex(c["pvk"])
    person = S2["ids"]["person_alice"]
    # the vector's salt is fixed; the construction is the same SALTED-AEAD
    k = crypto.hkdf(pvk, b"", f"orch/v2/label|{person}".encode())
    assert k.hex() == c["label_key"]
    blob = crypto.unb64u(c["sealed"])
    assert open_device_label(pvk, bytes.fromhex(person), bytes.fromhex(c["device_id"]), blob) == c["label"]
    again = seal_device_label(pvk, bytes.fromhex(person), bytes.fromhex(c["device_id"]), c["label"])
    assert again != blob  # fresh salt
    assert open_device_label(pvk, bytes.fromhex(person), bytes.fromhex(c["device_id"]), again) == c["label"]


def test_ids_vectors():
    ids = S2["ids"]
    assert crypto.person_id(pub("person_alice.sig")).hex() == ids["person_alice"]
    assert crypto.person_id(pub("person_bob.sig")).hex() == ids["person_bob"]
    assert crypto.device_id(pub("primary.sig")).hex() == ids["device_primary"]
    assert crypto.device_id(pub("phone.sig")).hex() == ids["device_phone"]
    assert crypto.device_id(pub("laptop.sig")).hex() == ids["device_laptop"]
    assert crypto.device_id(pub("intruder.sig")).hex() == ids["device_intruder"]
    assert crypto.device_id(pub("agent.sig")).hex() == ids["device_agent"]
    assert crypto.device_id(pub("bob_phone.sig")).hex() == ids["device_bob_phone"]
    assert crypto.pk_pin(pub("person_alice.sig")).hex() == ids["pk_pin_alice"]
    assert crypto.wsk_pin(pub("wsk_a.sig")).hex() == ids["wsk_pin_a"]


@pytest.mark.parametrize("case", ALL["encodings"]["b64u"], ids=lambda c: c["name"])
def test_b64u_vectors(case):
    if case["bytes"] is not None:
        assert crypto.unb64u(case["text"]) == bytes.fromhex(case["bytes"])
    else:
        with pytest.raises(EncodingError):
            crypto.unb64u(case["text"])


def test_key_pair_round_trip_on_vector_keys():
    k = priv("phone.sig")
    sig = crypto.sign(k, b"x")
    assert crypto.verify(pub("phone.sig"), sig, b"x")
    assert isinstance(k, ec.EllipticCurvePrivateKey)


CONSUMED = {"keys", "ids", "sign", "hkdf", "salted_aead", "seal", "seal_open", "label_sealed", "certs", "revocations"}
OTHER_TESTS = {"question_hash", "decisions", "webauthn", "publish"}
DEFERRED = {
    "card", "wk_grants", "member_lists", "cert_request", "enroll", "bridge", "revocation_op", "push", "drop",
    "ws_envelopes", "relay_auth",
}  # fmt: skip


def test_deferred_sections_are_complete():
    sections = set(S2) - {"suite", "name"}
    assert sections == CONSUMED | OTHER_TESTS | DEFERRED, sections ^ (CONSUMED | OTHER_TESTS | DEFERRED)
    assert not CONSUMED & DEFERRED
