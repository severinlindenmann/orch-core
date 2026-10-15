"""Review round 1 of #495 (Opus L1, Codex P2): every R6 check refuses a **signed** object whose ids or fields have the
wrong JSON type (a list, an object, a number, null) with :class:`Refused`, never a ``TypeError`` or ``KeyError``.
The signatures are valid (corpus keys), so each value reaches the check's shape logic."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.identity import Refused, certreq, memberlist, wscard

from ..relay import corpus

S2 = corpus.S2
IDS = S2["ids"]
CR = S2["cert_request"]
REQ_O = CR["signed"]["o"]
CH_O = CR["challenge"]["o"]
RJ_O = S2["rejected_forms"]["objects"]["rejection"]["o"]
WS = IDS["workspace_a"]
PK = corpus.pub("person_alice.sig")
WSK = corpus.pub("wsk_a.sig")
CARD = wscard.verify_card(CR["cases"][0]["card"], PK)
BAD = [["a"], {"a": 1}, 7, None, True]


def _refused_or_accepted(call, original: object) -> None:
    """A string field (an id, a key, a hash) of another type is always refused; for a number field a value of the
    right type may be valid, so the call either refuses or returns. Never any other exception."""
    try:
        call()
    except Refused:
        return
    assert type(original) is not str, "a non-string value for a string field was accepted"


def _sign(name: str, label: str, o: dict) -> dict:
    return {"o": o, "sig": crypto.b64u(crypto.sign(corpus.priv(name), crypto.L[label] + canon.dumps(o)))}


def _variants(o: dict, skip: tuple[str, ...] = ("v", "suite", "kind")) -> list[tuple[str, dict, object]]:
    return [(f"{k}={bad!r}", {**o, k: bad}, o[k]) for k in o if k not in skip for bad in BAD]


REQ_V = _variants(REQ_O)


@pytest.mark.parametrize(("name", "o", "orig"), REQ_V, ids=[n for n, _, _ in REQ_V])
def test_check_cert_request_refuses_bad_types(name, o, orig):
    _refused_or_accepted(
        lambda: certreq.check_cert_request(
            _sign("wsk_a.sig", "sig_cert_request", o),
            CARD,
            me_person_id=IDS["person_alice"],
            me_device_id=IDS["device_primary"],
            journal=certreq.CertJournal(seen=frozenset({"x"})),
            now_ms=REQ_O["created_ms"],
        ),
        orig,
    )


CH_V = _variants(CH_O)
RJ_V = _variants(RJ_O)


@pytest.mark.parametrize(("name", "o", "orig"), CH_V, ids=[n for n, _, _ in CH_V])
def test_check_cert_challenge_refuses_bad_types(name, o, orig):
    signed = _sign("person_alice.sig", "sig_cert_challenge", o)
    _refused_or_accepted(lambda: certreq.check_cert_challenge(signed, PK, REQ_O), orig)


@pytest.mark.parametrize(("name", "o", "orig"), RJ_V, ids=[n for n, _, _ in RJ_V])
def test_check_cert_rejection_refuses_bad_types(name, o, orig):
    signed = _sign("person_alice.sig", "sig_cert_rejection", o)
    _refused_or_accepted(lambda: certreq.check_cert_rejection(signed, PK, REQ_O), orig)


@pytest.mark.parametrize("bad", BAD, ids=repr)
@pytest.mark.parametrize("key", sorted(REQ_O))
def test_request_o_of_bad_type_is_refused_by_the_host_checks(key, bad):
    """The host's own request object is checked too (it comes from the caller's store)."""
    _refused_or_accepted(lambda: certreq.check_cert_challenge(CR["challenge"], PK, {**REQ_O, key: bad}), REQ_O[key])


INNER = CR["inner"]
INNER_V = [(f"{k}={bad!r}", {**INNER, k: bad}, INNER[k]) for k in INNER for bad in BAD]
_PRIMARY_KX = corpus.priv("primary.kx")


@pytest.mark.parametrize(("name", "inner", "orig"), INNER_V, ids=[n for n, _, _ in INNER_V])
def test_open_cert_request_refuses_bad_inner_types(name, inner, orig):
    rid = bytes.fromhex(REQ_O["request_id"])
    sealed = crypto.seal(
        corpus.pub("primary.kx"), bytes.fromhex(IDS["device_primary"]), "cert-request", rid, 0, canon.dumps(inner)
    )
    o = {**REQ_O, "sealed": crypto.b64u(sealed)}
    req = certreq.check_cert_request(
        _sign("wsk_a.sig", "sig_cert_request", o),
        CARD,
        me_person_id=IDS["person_alice"],
        me_device_id=IDS["device_primary"],
        journal=certreq.CertJournal(),
        now_ms=REQ_O["created_ms"],
    )

    def unseal(object_id: bytes, blob: bytes) -> bytes:
        return crypto.open_sealed(_PRIMARY_KX, bytes.fromhex(IDS["device_primary"]), "cert-request", object_id, 0, blob)

    _refused_or_accepted(lambda: certreq.open_cert_request(req, unseal), orig)


LIST_O = next(c for c in S2["member_lists"] if c["name"] == "first_list")["signed"]["o"]
MEMBER = LIST_O["members"][0]
LIST_V = [
    *_variants(LIST_O),
    *(
        (f"members[0].{k}={bad!r}", {**LIST_O, "members": [{**MEMBER, k: bad}]}, MEMBER[k])
        for k in MEMBER
        for bad in BAD
    ),
    *((f"persons=[{bad!r}]", {**LIST_O, "persons": [bad]}, "") for bad in BAD),
    *((f"members=[{bad!r}]", {**LIST_O, "members": [bad]}, "") for bad in BAD),
]


@pytest.mark.parametrize(("name", "o", "orig"), LIST_V, ids=[n for n, _, _ in LIST_V])
def test_check_member_list_refuses_bad_types(name, o, orig):
    signed = _sign("wsk_a.sig", "sig_member_list", o)
    _refused_or_accepted(lambda: memberlist.check_member_list(signed, WSK, WS), orig)


GRANT_O = S2["wk_grants"]["grant_phone_epoch_1"]["o"]
GRANT_V = _variants(GRANT_O)


@pytest.mark.parametrize(("name", "o", "orig"), GRANT_V, ids=[n for n, _, _ in GRANT_V])
def test_check_wk_grant_refuses_bad_types(name, o, orig):
    signed = _sign("wsk_a.sig", "sig_wk_grant", o)
    _refused_or_accepted(lambda: memberlist.check_wk_grant(signed, WSK, WS, IDS["device_phone"]), orig)


@pytest.mark.parametrize("bad", BAD, ids=repr)
def test_caller_ids_of_bad_type_are_refused(bad):
    signed = S2["wk_grants"]["grant_phone_epoch_1"]
    for call in (
        lambda: memberlist.check_wk_grant(signed, WSK, bad, IDS["device_phone"]),
        lambda: memberlist.check_wk_grant(signed, WSK, WS, bad),
        lambda: memberlist.check_member_list({"o": LIST_O, "sig": "x"}, WSK, bad),
    ):
        with pytest.raises(Refused):
            call()
