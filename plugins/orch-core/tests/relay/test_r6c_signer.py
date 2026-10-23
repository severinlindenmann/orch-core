"""R6c: the two ``RelaySigner`` methods pairing adds (11A.15.7): ``pair_answer`` (ORB2 direction 2, epoch 0, an offer
R6 holds live, the 64 KiB envelope cap) and ``cert_request`` (exact fields, the pinned owner, the verified primary,
the 10-minute bound, one request per ``request_id`` in the ledger)."""

from __future__ import annotations

import secrets

import pytest

from orch import crypto
from orch.crypto.labels import L
from orch.identity import certreq
from orch.wsproto import sign, wire
from orch.wsproto.sign import SignerRefused

from . import corpus
from .helpers import WS, WSK_PUB, Key, MemLedger

ORIGIN = "https://relay.example"
OWNER = corpus.IDS["person_alice"]
OFFER = "a078a2b2aaadd8b9936f0260104de707"
PRIMARY_KX = crypto.public_bytes(crypto.generate_private_key())
PRIMARY_DEV = "6a29d184e6be89bf89cb4864ecd6a507"
PHONE_SIG = crypto.public_bytes(crypto.generate_private_key())
PHONE_KX = crypto.public_bytes(crypto.generate_private_key())


BOUND: set[tuple[str, str, str, str]] = set()


def bound(offer, attempt, device, dk_hash):
    return (offer, attempt, device, dk_hash) in BOUND


def signer(*, live=lambda offer: offer == OFFER, primary=lambda: PRIMARY_DEV, ledger=None, owner=OWNER,
           binding=bound):  # fmt: skip
    return sign.RelaySigner(
        Key(), sign.LinkContext(WS, ORIGIN), ledger=ledger if ledger is not None else MemLedger(),
        owner_person_id=owner, primary_device=primary, pair_live=live, pair_binding=binding,
    )  # fmt: skip


def header(*, direction=2, epoch=0, stream=OFFER, ws=WS, flags=wire.FLAG_LAST, seq=0) -> bytes:
    return wire.Header(
        2, direction, flags, crypto.SUITE_ID, bytes.fromhex(ws), secrets.token_bytes(16), secrets.token_bytes(16),
        bytes.fromhex(stream), seq, 1_790_000_000_000, epoch, secrets.token_bytes(16),
    ).pack()  # fmt: skip


def request_o(*, bind=True, **over):
    o = certreq.cert_request_o(
        request_id=secrets.token_hex(16), workspace_id=WS, person_id=OWNER, primary_device_id=PRIMARY_DEV,
        primary_kx_pub=PRIMARY_KX, offer_id=OFFER, attempt=secrets.token_hex(16), dk_sig_pub=PHONE_SIG,
        dk_kx_pub=PHONE_KX, label="phone", scopes_max=["look", "decide"], created_ms=1_790_000_000_000,
    )  # fmt: skip
    if bind:
        BOUND.add((o["offer_id"], o["attempt"], o["device_id"], o["dk_hash"]))
    o.update(over)
    return o


# -- pair_answer -----------------------------------------------------------------------------------------------------


def test_pair_answer_signs_an_epoch_0_answer_for_a_live_offer():
    s = signer()
    h, body = header(), b"x" * 40
    sig = s.pair_answer(h, body)
    assert crypto.verify(WSK_PUB, sig, L["sig_bridge"] + h + body)


@pytest.mark.parametrize(
    "kw",
    [
        {"direction": 1, "flags": 0},  # a request header
        {"epoch": 1},  # a bridge chunk is never a pair answer
        {"stream": "00" * 16},  # no offer
        {"stream": "ab" * 16},  # an offer R6 does not hold live
        {"ws": "cd" * 16},  # another workspace
        {"seq": 1},  # an answer is one chunk, index 0
        {"flags": wire.FLAG_STREAM | wire.FLAG_LAST},  # never a stream frame
        {"flags": 0},  # not LAST
    ],
)
def test_pair_answer_refuses_anything_but_a_live_offers_epoch_0_answer(kw):
    with pytest.raises(SignerRefused):
        signer().pair_answer(header(**kw), b"x" * 40)


def test_pair_answer_refuses_an_envelope_over_64_kib_and_without_liveness():
    with pytest.raises(SignerRefused):
        signer().pair_answer(header(), b"x" * (65_536 - wire.HEADER_LEN - wire.SIG_LEN + 1))
    signer().pair_answer(header(), b"x" * (65_536 - wire.HEADER_LEN - wire.SIG_LEN))
    with pytest.raises(SignerRefused):
        signer(live=None).pair_answer(header(), b"x" * 40)


def test_response_chunk_still_refuses_epoch_0():
    s = sign.RelaySigner(Key(), sign.LinkContext(WS, ORIGIN))
    with pytest.raises(SignerRefused):
        s.response_chunk(header(), b"x" * 40)


# -- cert_request ----------------------------------------------------------------------------------------------------


def test_cert_request_signs_and_the_signature_verifies_and_is_recorded_once():
    led = MemLedger()
    s = signer(ledger=led)
    o = request_o()
    sig = s.cert_request(o)
    signed = {"o": o, "sig": sig}
    assert certreq._verify_signed(WSK_PUB, signed, "cert_request") == o
    assert s.cert_request(o) == sig  # the same bytes: the recorded signature (ECDSA is randomised)
    assert led.get("cert_request", o["request_id"]) is not None


def test_cert_request_refuses_other_bytes_under_a_recorded_request_id():
    s = signer()
    o = request_o()
    s.cert_request(o)
    with pytest.raises(SignerRefused):
        s.cert_request({**o, "created_ms": o["created_ms"] + 1, "expires_ms": o["expires_ms"] + 1})


@pytest.mark.parametrize(
    "over",
    [
        {"person_id": "ef" * 16},  # not the pinned owner
        {"primary_device_id": "12" * 16},  # not the verified primary
        {"workspace_id": "cd" * 16},
        {"expires_ms": 1_790_000_600_001},  # longer than 10 minutes
        {"kind": "cert_challenge"},
        {"extra": 1},
    ],
)
def test_cert_request_refuses_a_request_outside_its_context(over):
    with pytest.raises(SignerRefused):
        signer().cert_request(request_o(**over))


def test_cert_request_refuses_without_a_verified_primary_or_ledger():
    with pytest.raises(SignerRefused):
        signer(primary=lambda: None).cert_request(request_o())
    s = sign.RelaySigner(
        Key(), sign.LinkContext(WS, ORIGIN), owner_person_id=OWNER, primary_device=lambda: PRIMARY_DEV,
        pair_binding=bound,
    )  # fmt: skip
    with pytest.raises(SignerRefused, match="ledger"):
        s.cert_request(request_o())


def test_the_ledger_accepts_cert_request_keys_only_as_32_hex():
    from orch.wsproto.state import _ledger_key_ok

    assert _ledger_key_ok("cert_request", "ab" * 16)
    assert not _ledger_key_ok("cert_request", "AB" * 16)
    assert not _ledger_key_ok("cert_request", "1")


def test_the_cert_request_field_set_is_certreqs():
    assert sign.CERT_REQUEST_FIELDS == certreq.REQUEST_FIELDS


def test_pair_answer_accepts_exactly_last_and_last_refusal():
    s = signer()
    s.pair_answer(header(flags=wire.FLAG_LAST), b"x" * 40)
    s.pair_answer(header(flags=wire.FLAG_LAST | wire.FLAG_REFUSAL), b"x" * 40)


def test_a_signed_pair_answer_does_not_verify_for_another_offer_or_device():
    """The whole header is signed: the offer (stream), the device and the rid are bound to the signature."""
    s = signer()
    h, body = header(), b"x" * 40
    sig = s.pair_answer(h, body)
    other = bytearray(h)
    other[56:72] = bytes.fromhex("ab" * 16)  # stream: another offer
    assert not crypto.verify(WSK_PUB, sig, L["sig_bridge"] + bytes(other) + body)
    other = bytearray(h)
    other[24:40] = bytes(16)  # another device
    assert not crypto.verify(WSK_PUB, sig, L["sig_bridge"] + bytes(other) + body)


@pytest.mark.parametrize("field", ["offer_id", "attempt", "device_id", "dk_hash"])
def test_cert_request_refuses_a_binding_r6_does_not_hold(field):
    """Cross-context: a request for another offer, attempt, device or key pair than the pairing R6 holds at 7b."""
    o = request_o(bind=False)
    with pytest.raises(SignerRefused):
        signer().cert_request(o)
    BOUND.add((o["offer_id"], o["attempt"], o["device_id"], o["dk_hash"]))
    replacement = {"offer_id": "ab" * 16, "attempt": "cd" * 16, "device_id": "ef" * 16, "dk_hash": "A" * 43}[field]
    with pytest.raises(SignerRefused):
        signer().cert_request({**o, field: replacement})
    signer().cert_request(o)


def test_cert_request_refuses_without_the_binding_callback():
    with pytest.raises(SignerRefused):
        signer(binding=None).cert_request(request_o())
