"""Review round 2 of #495 (Opus D1-1, D1-2) and #501 (Opus D2-2): a checked result type (``VerifiedCard``,
``CertRequest``, ``CertInner``, ``CertChallenge``, ``CertRejection``) carries a mint bound to its whole content.
``dataclasses.replace`` or any field change is refused, a forged instance is refused where it is consumed, subclassing
is refused, and the stored objects are read-only copies. ``copy``, ``deepcopy`` and ``pickle`` keep a valid instance
valid."""

from __future__ import annotations

import copy
import dataclasses
import pickle

import pytest

from orch import crypto
from orch.identity import Refused, answers, certreq, wscard

from ..wsproto import corpus

S2 = corpus.S2
CR = S2["cert_request"]
REQ = CR["signed"]
PK = corpus.pub("person_alice.sig")
ATTACKER = corpus.pub("wsk_intruder.sig")
OBJ = S2["rejected_forms"]["objects"]
W = corpus.pub("wsk_a.sig")
ME = {"me_person_id": S2["ids"]["person_alice"], "me_device_id": S2["ids"]["device_primary"]}


def _card() -> wscard.VerifiedCard:
    return wscard.verify_card(CR["cases"][0]["card"], PK)


def _challenge() -> certreq.CertChallenge:
    return certreq.check_cert_challenge(CR["challenge"], PK, REQ["o"])


def _rejection() -> certreq.CertRejection:
    return certreq.check_cert_rejection(OBJ["rejection"], PK, OBJ["request"]["o"])


def _use_card(card) -> certreq.CertRequest:
    return certreq.check_cert_request(REQ, card, journal=certreq.CertJournal(), now_ms=REQ["o"]["created_ms"], **ME)


def _kx_unseal(object_id: bytes, sealed: bytes) -> bytes:
    keys = S2["keys"]
    seed = int.from_bytes(bytes.fromhex(keys["primary.kx"]["seed"]), "big")
    kx = crypto.private_key_from_scalar(seed % (crypto.P256_N - 1) + 1)
    return crypto.open_sealed(kx, bytes.fromhex(S2["ids"]["device_primary"]), "cert-request", object_id, 0, sealed)


def _alice_sign(payload: bytes) -> bytes:
    seed = int.from_bytes(bytes.fromhex(S2["keys"]["person_alice.sig"]["seed"]), "big")
    return crypto.sign(crypto.private_key_from_scalar(seed % (crypto.P256_N - 1) + 1), payload)


def _request() -> certreq.CertRequest:
    return _use_card(_card())


def _inner() -> certreq.CertInner:
    return certreq.open_cert_request(_request(), _kx_unseal)


def _use_request(req) -> certreq.CertInner:
    return certreq.open_cert_request(req, _kx_unseal)


def _use_inner(inner) -> dict:
    return certreq.make_cert_challenge(PK, _alice_sign, _request(), inner, bytes(32))


def _use_challenge(ch) -> dict:
    return answers.answer_cert_pending(W, primary_label="Mac", pk_pub=PK, challenge=ch)


def _use_rejection(rj) -> dict:
    return answers.answer_rejected_long(W, rejection=rj)


CASES = [
    ("card", _card, _use_card, {"wsk_pub": ATTACKER}),
    ("card_o", _card, _use_card, {"o_cj": b"{}"}),
    ("card_seq", _card, _use_card, {"card_seq": 99}),
    ("request", _request, _use_request, {"dk_hash": bytes(32)}),
    ("request_o", _request, _use_request, {"o_cj": b"{}"}),
    ("inner_keys", _inner, _use_inner, {"dk_sig_pub": ATTACKER}),
    ("inner_request", _inner, _use_inner, {"request_id": "0" * 32}),
    ("inner_scopes", _inner, _use_inner, {"scopes_cj": b'["type"]'}),
    ("challenge", _challenge, _use_challenge, {"signed_cj": b'{"o":{},"sig":"x"}'}),
    ("challenge_code", _challenge, _use_challenge, {"cert_code": "AAAAAA"}),
    ("rejection", _rejection, _use_rejection, {"signed_cj": b'{"o":{},"sig":"x"}'}),
    ("rejection_pk", _rejection, _use_rejection, {"pk_pub": ATTACKER}),
]


@pytest.mark.parametrize(("name", "make", "use", "change"), CASES, ids=[c[0] for c in CASES])
def test_replace_is_refused(name, make, use, change):
    with pytest.raises(Refused) as e:
        dataclasses.replace(make(), **change)
    assert e.value.code == "malformed"


@pytest.mark.parametrize(("name", "make", "use", "change"), CASES, ids=[c[0] for c in CASES])
def test_a_forged_instance_is_refused_where_it_is_consumed(name, make, use, change):
    """Bypassing ``__init__`` (``object.__setattr__`` on a genuine instance's copy) still fails at the consumer."""
    obj = copy.copy(make())
    for k, v in change.items():
        object.__setattr__(obj, k, v)
    with pytest.raises(Refused) as e:
        use(obj)
    assert e.value.code == "malformed"


@pytest.mark.parametrize(("name", "make", "use", "change"), CASES[::3], ids=[c[0] for c in CASES[::3]])
@pytest.mark.parametrize(
    "dup", [copy.copy, copy.deepcopy, lambda x: pickle.loads(pickle.dumps(x))], ids=["copy", "deepcopy", "pickle"]
)
def test_copies_stay_valid(name, make, use, change, dup):
    use(dup(make()))


@pytest.mark.parametrize(
    "cls",
    [wscard.VerifiedCard, certreq.CertRequest, certreq.CertInner, certreq.CertChallenge, certreq.CertRejection],
)
def test_subclassing_is_refused(cls):
    with pytest.raises(TypeError):
        type("Sub", (cls,), {})


def test_stored_objects_are_read_only_copies():
    card = _card()
    card.o["wsk_pub"] = crypto.b64u(ATTACKER)
    assert card.o["wsk_pub"] != crypto.b64u(ATTACKER)
    ch = _challenge()
    ch.signed["o"]["device_id"] = "0" * 32
    assert ch.signed == CR["challenge"]
    _use_card(card)
    _use_challenge(ch)


def test_a_hand_built_inner_is_refused():
    """#501 D2-2: an inner built by hand (attacker keys, the honest request's binding) never reaches the PK signer."""
    honest = _inner()
    fields = {f.name: getattr(honest, f.name) for f in dataclasses.fields(honest) if f.name != "mint"}
    with pytest.raises(Refused) as e:
        certreq.CertInner(**{**fields, "dk_sig_pub": ATTACKER})
    assert e.value.code == "malformed"
    req = {f.name: getattr(_request(), f.name) for f in dataclasses.fields(certreq.CertRequest) if f.name != "mint"}
    with pytest.raises(Refused) as e:
        certreq.CertRequest(**req)
    assert e.value.code == "malformed"


def test_the_inner_carries_its_request_id():
    req = _request()
    inner = certreq.open_cert_request(req, _kx_unseal)
    assert inner.request_id == req.request_id and inner.scopes_max == tuple(CR["inner"]["scopes_max"])
