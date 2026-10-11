"""Pairing attempt binding (orch-relay protocol-v2 §8.3/§8.4, P3-D28), checked from the spec text alone.

``dk_hash`` and the two signatures are recomputed here from the formulas in the protocol, not by calling relay code;
the vectors (``cert_binding``, ``rejected_forms``, ``approved_anchor``) only supply the inputs and the expected values.
The relay, host and primary state machines of those groups belong to the P3 bridge and are not consumed here.
"""

from __future__ import annotations

import hashlib

from orch import canon, crypto
from orch.crypto.labels import L

from .vectors import S2, pub

OBJ = S2["rejected_forms"]["objects"]


def lp16(x: bytes) -> bytes:
    return len(x).to_bytes(2, "big") + x


def dk_hash(sig_pub: bytes, kx_pub: bytes) -> str:
    """``b64u(H("orch/v2/dk-hash|" || suite || lp16(dk_sig_pub) || lp16(dk_kx_pub)))`` (§8.4)."""
    return crypto.b64u(hashlib.sha256(b"orch/v2/dk-hash|" + bytes([2]) + lp16(sig_pub) + lp16(kx_pub)).digest())


def test_dk_hash_label_and_formula():
    assert L["h_dk_hash"] == b"orch/v2/dk-hash|"
    want = OBJ["request"]["o"]["dk_hash"]
    assert dk_hash(pub("phone.sig"), pub("phone.kx")) == want == OBJ["rejection"]["o"]["dk_hash"]
    assert dk_hash(pub("phone.sig"), pub("intruder.kx")) != want  # both keys are bound, not only the signing key
    assert dk_hash(pub("phone.kx"), pub("phone.sig")) != want  # and in this order


def test_request_and_rejection_carry_the_same_attempt_binding():
    req, rej = OBJ["request"]["o"], OBJ["rejection"]["o"]
    assert crypto.device_id(pub("phone.sig")).hex() == req["device_id"]
    for f in ("request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id", "dk_hash", "expires_ms"):
        assert req[f] == rej[f], f
    assert len(req["attempt"]) == 32 and req["attempt"] == req["attempt"].lower()


def test_cert_rejection_is_signed_by_pk_under_its_own_label():
    o, sig = OBJ["rejection"]["o"], crypto.unb64u(OBJ["rejection"]["sig"])
    assert L["sig_cert_rejection"] == b"orch/v2/sig/cert-rejection|"
    assert crypto.verify(pub("person_alice.sig"), sig, L["sig_cert_rejection"] + canon.cj_checked(o))
    assert not crypto.verify(pub("person_alice.sig"), sig, L["sig_cert_challenge"] + canon.cj_checked(o))
    assert not crypto.verify(pub("person_bob.sig"), sig, L["sig_cert_rejection"] + canon.cj_checked(o))
    assert crypto.person_id(pub("person_alice.sig")).hex() == o["person_id"]


def test_cert_request_clear_binding_is_signed_by_wsk():
    o, sig = OBJ["request"]["o"], crypto.unb64u(OBJ["request"]["sig"])
    assert crypto.verify(pub("wsk_a.sig"), sig, L["sig_cert_request"] + canon.cj_checked(o))
    tampered = {**o, "attempt": "0" * 32}
    assert not crypto.verify(pub("wsk_a.sig"), sig, L["sig_cert_request"] + canon.cj_checked(tampered))


def test_the_person_key_may_sign_a_rejection_and_the_device_key_may_not():
    from orch.custody.base import DEVICE_KEY_LABELS, PERSON_KEY_LABELS

    assert L["sig_cert_rejection"] in PERSON_KEY_LABELS
    assert L["sig_cert_rejection"] not in DEVICE_KEY_LABELS
