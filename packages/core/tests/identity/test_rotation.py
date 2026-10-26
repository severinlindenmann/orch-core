"""#1097: the person key's ``device_rotation`` (label ``orch/v2/sig/device-rotate|``) and the verifier of a
``device.rotated`` event: the new device's certificate and the fresh rotation, both under the person key."""

from __future__ import annotations

import copy

import pytest

from orch import crypto
from orch.identity import CryptoVerifier, Refused, certs

from .helpers import WS, Person


def rotated_event(owner: Person, new: Person, *, ws=WS, roster_v=3, old=None):
    old = old or owner.device
    rotation = certs.make_rotation(
        owner.pk_pub, owner.pk_sign, workspace_id=ws, old_hex=old[2:], new_hex=new.device[2:], roster_v=roster_v
    )
    return {
        "type": "device.rotated",
        "old": old,
        "device": new.device,
        "cert": new.cert_by(owner),
        "rotation": rotation,
    }


@pytest.fixture
def people(monkeypatch):
    owner, new = Person(), Person()

    def cert_by(self, signer):
        return certs.make_device_cert(
            signer.pk_pub, signer.pk_sign, dk_sig_pub=self.sig_pub, dk_kx_pub=self.kx_pub, label_sealed=b"x",
            created_ms=1_790_000_000_000, expires_ms=None, scopes_max=["look", "decide", "operate", "type"],
        )  # fmt: skip

    monkeypatch.setattr(Person, "cert_by", cert_by, raising=False)
    return owner, new


def test_a_valid_rotation_verifies_under_the_person_key(people):
    owner, new = people
    e = rotated_event(owner, new)
    assert CryptoVerifier().verify_embedded(e, pk_pub=crypto.b64u(owner.pk_pub))
    o = certs.check_rotation(e["rotation"], owner.pk_pub)
    assert o["workspace_id"] == WS and o["roster_v"] == 3 and o["new"] == new.device[2:]


def test_the_rotation_label_is_the_person_key_label_and_not_a_certificate(people):
    owner, _ = people
    from orch.crypto.labels import L

    assert L["sig_device_rotate"] == b"orch/v2/sig/device-rotate|" and L["sig_device_rotate"] != L["sig_device_cert"]


def test_a_rotation_signed_by_another_key_or_with_a_changed_field_does_not_verify(people):
    owner, new = people
    e = rotated_event(owner, new)
    stranger = Person()
    v = CryptoVerifier()
    assert not v.verify_embedded(e, pk_pub=crypto.b64u(stranger.pk_pub))
    forged = copy.deepcopy(e)
    forged["rotation"]["o"]["roster_v"] = 99  # the signature covers the roster version
    assert not v.verify_embedded(forged, pk_pub=crypto.b64u(owner.pk_pub))
    forged = copy.deepcopy(e)
    forged["rotation"]["o"]["workspace_id"] = "ab" * 16  # ... and the workspace
    assert not v.verify_embedded(forged, pk_pub=crypto.b64u(owner.pk_pub))
    swapped = copy.deepcopy(e)
    swapped["rotation"] = {**swapped["rotation"], "o": {**swapped["rotation"]["o"], "kind": "revocation"}}
    assert not v.verify_embedded(swapped, pk_pub=crypto.b64u(owner.pk_pub))


def test_a_certificate_signature_is_not_a_rotation_and_the_cert_must_be_the_new_devices(people):
    owner, new = people
    e = rotated_event(owner, new)
    v = CryptoVerifier()
    as_rotation = copy.deepcopy(e)
    as_rotation["rotation"] = e["cert"]  # a device certificate is not a rotation
    assert not v.verify_embedded(as_rotation, pk_pub=crypto.b64u(owner.pk_pub))
    other = copy.deepcopy(e)
    other["device"] = Person().device  # the event names a device the certificate is not for
    assert not v.verify_embedded(other, pk_pub=crypto.b64u(owner.pk_pub))


def test_check_rotation_refuses_another_person_and_malformed_objects(people):
    owner, new = people
    e = rotated_event(owner, new)
    with pytest.raises(Refused):
        certs.check_rotation(e["rotation"], Person().pk_pub)
    with pytest.raises(Refused):
        certs.check_rotation({"o": e["rotation"]["o"]}, owner.pk_pub)
