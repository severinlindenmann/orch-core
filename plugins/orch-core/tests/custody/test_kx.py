"""Custody of key-exchange keys (orch-v2-host 11A.15.10 item 9, C2): the ``kx`` key type with per-key purposes in the
authenticated header, ``Backend.unseal``, and the ``primary`` signing role (relay login only)."""

from __future__ import annotations

import json
import os

import pytest
from cryptography.exceptions import InvalidTag

from orch import crypto
from orch.crypto.labels import L
from orch.custody import (
    PLANNED,
    PRIMARY_SIG_LABELS,
    CustodyError,
    FileBackend,
    KdfParams,
    PassphraseBackend,
    UnavailableBackend,
    WrongPassphrase,
)
from orch.custody import passphrase as pp
from orch.custody.base import BackendUnavailable
from orch.custody.convenience import ConvenienceBackend, FakeStore

FAST = KdfParams(n=2**10)
PHRASE = "Zq7!mPx2-vL9#rTb4w"
RCPT = bytes.fromhex("11" * 16)
OBJ = bytes.fromhex("22" * 16)


def backend(tmp_path, phrase=PHRASE):
    seen = []

    def provider(req):
        seen.append(req)
        return phrase

    b = PassphraseBackend(tmp_path / "keys", _passphrase_provider=provider, _kdf=FAST, _min_n=2**10)
    b.seen = seen  # type: ignore[attr-defined]
    return b


def kx(b, key_id="kx", purposes=("cert-request",), recipient=RCPT):
    """A kx key with a chosen recipient, through the private creator (the public ``create`` refuses kx keys)."""
    return b._create_key(key_id, secret=None, role="kx", passphrase=None, purposes=purposes, recipient=recipient)


def make(b, key_id, role, **kw):
    """Any role through the private creator (``primary`` and ``kx`` are refused by ``create``)."""
    a = {"secret": None, "passphrase": None, "purposes": None, "recipient": None, **kw}
    return b._create_key(key_id, role=role, **a)


def relay_auth() -> bytes:
    return L["sig_relay_auth"] + b"\x02" + b"challenge"


def sealed(pub, purpose="cert-request", rcpt=RCPT, obj=OBJ, epoch=0, pt=b'{"x":1}', extra=b""):
    return crypto.seal(pub, rcpt, purpose, obj, epoch, pt, extra)


def test_kx_round_trip_opens_a_cert_request_seal(tmp_path):
    b = backend(tmp_path)
    pub = kx(b)
    assert b.unseal("kx", "cert-request", OBJ, 0, b"", sealed(pub)) == b'{"x":1}'
    assert b.seen[-1].kind == "unseal"
    assert ("purpose", "cert-request") in b.seen[-1].fields


def test_the_purposes_and_recipient_are_in_the_authenticated_header(tmp_path):
    b = backend(tmp_path)
    pub = kx(b)
    path = tmp_path / "keys" / "kx.key.json"
    doc = json.loads(path.read_text())
    assert doc["role"] == "kx" and doc["purposes"] == ["cert-request"] and doc["recipient"] == RCPT.hex()
    for field, value in (("purposes", ["wk"]), ("recipient", "33" * 16), ("role", "device")):
        bad = {**doc, field: value}
        path.chmod(0o600)
        os.unlink(path)
        path.write_text(json.dumps(bad))
        path.chmod(0o600)
        with pytest.raises(CustodyError):  # refused at load or the AEAD refuses the changed header
            b.unseal("kx", "wk" if field == "purposes" else "cert-request", OBJ, 0, b"", sealed(pub))
    os.unlink(path)


def test_a_purpose_the_key_does_not_hold_is_refused_before_any_prompt(tmp_path):
    b = backend(tmp_path)
    pub = kx(b)
    n = len(b.seen)
    for purpose in ("wk", "card", "ws-envelope", "vault", "nonsense"):
        with pytest.raises(CustodyError):
            b.unseal("kx", purpose, OBJ, 0, b"", sealed(pub))
    assert len(b.seen) == n


def test_cert_request_is_epoch_zero_and_no_extra(tmp_path):
    b = backend(tmp_path)
    pub = kx(b)
    n = len(b.seen)
    with pytest.raises(CustodyError):
        b.unseal("kx", "cert-request", OBJ, 1, b"", sealed(pub, epoch=1))
    with pytest.raises(CustodyError):
        b.unseal("kx", "cert-request", OBJ, 0, b"x", sealed(pub, extra=b"x"))
    assert len(b.seen) == n


def test_the_recipient_is_fixed_by_the_key(tmp_path):
    b = backend(tmp_path)
    pub = kx(b)
    with pytest.raises(InvalidTag):
        b.unseal("kx", "cert-request", OBJ, 0, b"", sealed(pub, rcpt=bytes(16)))


def test_a_bad_blob_raises_invalid_tag_and_a_bad_passphrase_raises_wrong_passphrase(tmp_path):
    b = backend(tmp_path)
    pub = kx(b)
    blob = bytearray(sealed(pub))
    blob[-1] ^= 1
    with pytest.raises(InvalidTag):
        b.unseal("kx", "cert-request", OBJ, 0, b"", bytes(blob))
    with pytest.raises(InvalidTag):
        b.unseal("kx", "cert-request", bytes(16), 0, b"", sealed(pub))
    other = PassphraseBackend(tmp_path / "keys", _passphrase_provider=lambda r: "nope nope", _kdf=FAST, _min_n=2**10)
    with pytest.raises(WrongPassphrase):
        other.unseal("kx", "cert-request", OBJ, 0, b"", sealed(pub))


def test_kx_never_signs_and_signing_keys_never_unseal(tmp_path):
    b = backend(tmp_path)
    kx(b)
    with pytest.raises(CustodyError):
        b.sign("kx", relay_auth(), action="a")
    for role in ("device", "person", "primary"):
        make(b, role, role)
        with pytest.raises(CustodyError):
            b.unseal(role, "cert-request", OBJ, 0, b"", b"\x04" * 81)


def test_kx_creation_rules(tmp_path):
    b = backend(tmp_path)
    with pytest.raises(CustodyError):  # a kx key names its purposes
        make(b, "a", "kx", recipient=RCPT)
    with pytest.raises(CustodyError):  # never ws-envelope, nor any purpose but cert-request in v1.7
        make(b, "b", "kx", purposes=("ws-envelope",), recipient=RCPT)
    with pytest.raises(CustodyError):
        make(b, "c", "kx", purposes=("cert-request", "wk"), recipient=RCPT)
    with pytest.raises(CustodyError):
        make(b, "d", "kx", purposes=("cert-request",), recipient=b"short")
    with pytest.raises(CustodyError):  # generated here, never imported
        make(b, "e", "kx", purposes=("cert-request",), recipient=RCPT, secret=bytes(31) + b"\x01")
    with pytest.raises(CustodyError):  # purposes belong to kx keys only
        make(b, "f", "device", purposes=("cert-request",))
    with pytest.raises(CustodyError):
        make(b, "g", "primary", secret=bytes(31) + b"\x01")
    assert not any(b.exists(k) for k in "abcdefg")


@pytest.mark.parametrize("role", ["primary", "kx"])
def test_the_public_create_refuses_primary_and_kx_keys(tmp_path, role):
    """Opus Low on #512: only ``create_primary`` makes them (own prompt, recipient = the pair's device id)."""
    b = backend(tmp_path)
    kw = {"purposes": ("cert-request",), "recipient": RCPT} if role == "kx" else {}
    with pytest.raises(CustodyError, match="create_primary"):
        b.create("x", role=role, passphrase=PHRASE, **kw)
    with pytest.raises(CustodyError, match="create_primary"):
        b.create("x", role=role, **kw)
    assert not b.exists("x") and b.seen == []


def test_create_primary_makes_the_pair(tmp_path):
    b = backend(tmp_path)
    sig_pub, kx_pub = b.create_primary("p", "kx", purposes=("cert-request",))
    assert [r.kind for r in b.seen] == ["create"]
    assert b.key_info("kx")["recipient"] == crypto.device_id(sig_pub).hex()
    assert b.key_info("p")["role"] == "primary" and crypto.unb64u(b.key_info("kx")["pub"]) == kx_pub


def test_old_key_files_keep_their_exact_shape(tmp_path):
    b = backend(tmp_path)
    b.create("dk")
    doc = json.loads((tmp_path / "keys" / "dk.key.json").read_text())
    assert set(doc) == {"v", "key_id", "role", "pub", "kdf", "nonce", "ct"}


def test_the_primary_role_signs_relay_logins_only(tmp_path):
    b = backend(tmp_path)
    pub = make(b, "p", "primary")
    assert PRIMARY_SIG_LABELS == (L["sig_relay_auth"],)
    with pytest.raises(CustodyError):
        b.sign("p", L["sig_device_cert"] + b"{}", action="a")
    with pytest.raises(CustodyError):
        b.sign("p", L["sig_decision"] + b"{}", action="a")
    del pub


def test_kx_and_primary_keys_are_never_exported_for_a_custody_change(tmp_path):
    b = backend(tmp_path)
    kx(b)
    make(b, "p", "primary")
    for key_id in ("kx", "p"):
        with pytest.raises(CustodyError):
            b.export_for_custody_change(key_id, fields=())


def test_a_batch_unlock_needs_a_device_key(tmp_path):
    b = backend(tmp_path)
    make(b, "p", "primary")
    b.create("pk", role="person")
    for key_id in ("p", "pk"):
        with pytest.raises(CustodyError):
            with b.unlocked(key_id, action="a", digest="ab" * 16, labels=(L["sig_relay_auth"],)):
                pass


def test_scalar_buffers_are_zeroised_after_unseal(tmp_path, monkeypatch):
    b = backend(tmp_path)
    pub = kx(b)
    buffers = []
    real = pp._zeroise
    monkeypatch.setattr(pp, "_zeroise", lambda buf: (buffers.append(buf), real(buf)))
    b.unseal("kx", "cert-request", OBJ, 0, b"", sealed(pub))
    assert buffers and all(not any(x) for x in buffers)


def test_other_backends_refuse_unseal():
    with pytest.raises(CustodyError):
        FileBackend.unseal(object.__new__(FileBackend), "k", "cert-request", OBJ, 0, b"", b"")
    with pytest.raises(BackendUnavailable):
        UnavailableBackend(next(iter(PLANNED))).unseal("k", "cert-request", OBJ, 0, b"", b"")
    key = crypto.generate_private_key()
    pub = crypto.public_bytes(key)
    from orch.identity import device_ref

    conv = ConvenienceBackend(FakeStore(), "705d40abbb8c1c90354a1acaa94c935c", device_ref(pub), pub)
    with pytest.raises(CustodyError):
        conv.unseal("dk", "cert-request", OBJ, 0, b"", b"")
