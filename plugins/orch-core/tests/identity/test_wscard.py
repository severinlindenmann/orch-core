"""H12c item 7's card builder and the typed verified card (design 11A.15.10; review round 1 of #495: Opus M1, Codex
P2), against the vendored relay corpus ``card`` section: every case, exact counts."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.identity import Refused, wscard
from orch.wsproto import card as relay_card

from ..relay import corpus

S2 = corpus.S2
CARD = S2["card"]
CASES = CARD["cases"]
CK = bytes.fromhex(CARD["card_key"])
PK = corpus.pub("person_alice.sig")
_b = crypto.unb64u


def _parts(o: dict) -> dict:
    return {
        "workspace_id": o["workspace_id"],
        "wsk_pub": _b(o["wsk_pub"]),
        "wxk_pub": _b(o["wxk_pub"]),
        "wxk_version": o["wxk_version"],
        "owner_person_id": o["owner_person_id"],
        "relay_url": o["relay_url"],
        "card_seq": o["card_seq"],
        "issued_ms": o["issued_ms"],
    }


def _opened(c: dict) -> dict | None:
    o = c["o"]
    try:
        return relay_card.open_card_part(CK, o["workspace_id"], o["card_seq"], _b(c["sealed"]))
    except relay_card.CardRefused:
        return None


def _consistent(c: dict) -> bool:
    """The card's sealed part opens under the corpus CK, is the one ``sealed_hash`` names, and ``ck_commit`` is that
    CK's: a card the builder makes from (CK, part)."""
    o = c["o"]
    return (
        _opened(c) is not None
        and relay_card.ck_commit(o["workspace_id"], o["card_seq"], CK) == o["ck_commit"]
        and relay_card.sealed_hash(_b(c["sealed"])) == o["sealed_hash"]
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_card_o_against_every_card_case(case):
    """A card whose sealed part and ``ck_commit`` come from the corpus CK is rebuilt byte for byte (``o`` and the
    sealed part); a bad ``relay_url`` is refused; any other card is not one the builder makes from (CK, part)."""
    c = case["card"]
    o = c["o"]
    if not relay_card.relay_url_ok(o["relay_url"]):
        with pytest.raises(Refused) as e:
            wscard.card_o(**_parts(o), ck=CK, part=CARD["sealed_part_plaintext"])
        assert e.value.code == "malformed"
        return
    if _consistent(c):
        built, sealed = wscard.card_o(**_parts(o), ck=CK, part=_opened(c))
        assert canon.dumps(built) == canon.dumps(o)
        assert sealed == _b(c["sealed"])
        return
    built, sealed = wscard.card_o(**_parts(o), ck=CK, part=_opened(c) or CARD["sealed_part_plaintext"])
    assert (built, sealed) != (o, _b(c["sealed"]))
    assert {k: v for k, v in built.items() if k not in ("sealed_hash", "ck_commit")} == {
        k: v for k, v in o.items() if k not in ("sealed_hash", "ck_commit")
    }


def test_card_cases_are_all_found():
    assert len(CASES) == 19
    bad_url = [c["name"] for c in CASES if not relay_card.relay_url_ok(c["card"]["o"]["relay_url"])]
    assert bad_url == ["relay_url_with_path", "relay_url_plain_http"]
    rebuilt = [
        c["name"] for c in CASES if relay_card.relay_url_ok(c["card"]["o"]["relay_url"]) and _consistent(c["card"])
    ]
    assert len(rebuilt) == 8
    assert "first_registration" in rebuilt and "relay_url_loopback_http" in rebuilt


def test_card_o_vector_card_and_seq_2_fields():
    built, sealed = wscard.card_o(**_parts(CARD["card"]["o"]), ck=CK, part=CARD["sealed_part_plaintext"])
    assert built == CARD["card"]["o"] and sealed.hex() == CARD["sealed_part"]
    assert crypto.L["sig_card_wsk"] + canon.dumps(built) == bytes.fromhex(CARD["signed_bytes_wsk"])


_O = CARD["card"]["o"]
_PART = CARD["sealed_part_plaintext"]


@pytest.mark.parametrize(
    "kw",
    [
        {"workspace_id": _O["workspace_id"].upper()},
        {"wsk_pub": b"\x04" + bytes(64)},
        {"wxk_pub": _O["wxk_pub"]},
        {"wxk_version": 0},
        {"owner_person_id": _O["owner_person_id"][:-2]},
        {"relay_url": "https://relay.example.org/"},
        {"card_seq": 0},
        {"card_seq": 2**32},
        {"issued_ms": -1},
        {"ck": bytes(31)},
        {"part": {**_PART, "extra": 1}},
        {"part": {**_PART, "hosted_by": 1}},
        {"part": {**_PART, "capabilities": "tickets"}},
        {"part": {**_PART, "name": None}},
    ],
)
def test_card_o_refuses(kw):
    a = {**_parts(_O), "ck": CK, "part": _PART, **kw}
    with pytest.raises(Refused) as e:
        wscard.card_o(**a)
    assert e.value.code == "malformed"


# --- the typed verified card (Opus M1) ------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_verify_card_matches_relay_verify_card(case):
    """The typed card exists only for a card that passes ``orch.wsproto.card.verify_card`` (checks 1 to 6); its
    refusals keep the relay's codes. Rollback (``accept_card``) stays the caller's."""
    pk = bytes.fromhex(case["owner_pk_pub"])
    try:
        want = relay_card.verify_card(case["card"], pk)
    except relay_card.CardRefused as e:
        with pytest.raises(Refused) as r:
            wscard.verify_card(case["card"], pk)
        assert r.value.code == e.code
        return
    got = wscard.verify_card(case["card"], pk)
    assert got.o == want and got.wsk_pub == _b(want["wsk_pub"]) and got.card_seq == want["card_seq"]
    assert got.client_hosted is case["card"]["delegation"]["o"]["client_hosted"]


def test_a_verified_card_cannot_be_constructed_directly():
    v = wscard.verify_card(CARD["card"], PK)
    with pytest.raises(Refused):
        wscard.VerifiedCard(
            workspace_id=v.workspace_id,
            owner_person_id=v.owner_person_id,
            wsk_pub=v.wsk_pub,
            card_seq=v.card_seq,
            relay_url=v.relay_url,
            client_hosted=v.client_hosted,
            o_cj=v.o_cj,
        )


def test_a_card_with_an_attackers_wsk_is_refused():
    """Opus M1: a card naming an attacker's WSK (signed by it) never yields a VerifiedCard under the owner's PK."""
    atk = corpus.priv("wsk_intruder.sig")
    o = {**CARD["card"]["o"], "wsk_pub": crypto.b64u(corpus.pub("wsk_intruder.sig"))}
    forged = {**CARD["card"], "o": o, "sig": crypto.b64u(crypto.sign(atk, relay_card.signed_bytes(o)))}
    with pytest.raises(Refused) as e:
        wscard.verify_card(forged, PK)
    assert e.value.code == "delegation_mismatch"
