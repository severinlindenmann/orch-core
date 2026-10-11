"""Pairing attempt binding (orch-relay protocol-v2 §8.3/§8.4, P3-D28), checked from the spec text alone.

``dk_hash`` and the two signatures are recomputed here from the formulas in the protocol, not by calling relay code;
the vectors (``cert_binding``, ``rejected_forms``, ``approved_anchor``) only supply the inputs and the expected values.
``dk_hash`` and the field bindings are checked in both suites (the suite byte comes from the vector). Core
implements only suite 2, so the signatures are verified there. The relay, host and primary state machines of
those groups belong to the P3 bridge and are not consumed here.
"""

from __future__ import annotations

import hashlib

import pytest

from orch import canon, crypto
from orch.crypto.labels import L

from .vectors import ALL, S2, pub

SUITES = ["1", "2"]


def lp16(x: bytes) -> bytes:
    return len(x).to_bytes(2, "big") + x


def dk_hash(suite: int, sig_pub: bytes, kx_pub: bytes) -> str:
    """``b64u(H("orch/v2/dk-hash|" || suite || lp16(dk_sig_pub) || lp16(dk_kx_pub)))`` (§8.4)."""
    return crypto.b64u(hashlib.sha256(b"orch/v2/dk-hash|" + bytes([suite]) + lp16(sig_pub) + lp16(kx_pub)).digest())


def _suite(sid: str) -> dict:
    return ALL["suites"][sid]


def _pub(sid: str, name: str) -> bytes:
    return bytes.fromhex(_suite(sid)["keys"][name]["pub"])


OBJ = S2["rejected_forms"]["objects"]


@pytest.mark.parametrize("sid", SUITES)
def test_dk_hash_label_and_formula(sid):
    assert L["h_dk_hash"] == b"orch/v2/dk-hash|"
    objs = _suite(sid)["rejected_forms"]["objects"]
    want = objs["request"]["o"]["dk_hash"]
    suite_byte = objs["request"]["o"]["suite"]
    assert (
        dk_hash(suite_byte, _pub(sid, "phone.sig"), _pub(sid, "phone.kx")) == want == objs["rejection"]["o"]["dk_hash"]
    )
    assert dk_hash(suite_byte, _pub(sid, "phone.sig"), _pub(sid, "intruder.kx")) != want  # both keys are bound
    assert dk_hash(suite_byte, _pub(sid, "phone.kx"), _pub(sid, "phone.sig")) != want  # and in this order


@pytest.mark.parametrize("sid", SUITES)
def test_request_and_rejection_carry_the_same_attempt_binding(sid):
    objs = _suite(sid)["rejected_forms"]["objects"]
    req, rej = objs["request"]["o"], objs["rejection"]["o"]
    for f in ("request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id", "dk_hash", "expires_ms"):
        assert req[f] == rej[f], f
    assert len(req["attempt"]) == 32 and req["attempt"] == req["attempt"].lower()
    assert set(rej) == {
        "v", "suite", "kind", "request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id", "dk_hash",
        "expires_ms",
    }  # fmt: skip


def test_device_id_of_the_attempt_keys():
    assert crypto.device_id(pub("phone.sig")).hex() == OBJ["request"]["o"]["device_id"]


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
