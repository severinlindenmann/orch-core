"""``orch.wsproto.card``: the card checks of proto §7.1 in the relay's order and the relay's update rules (every
``card.cases`` vector), and the sealed part, ``sealed_hash`` and ``ck_commit`` against the ``card`` vectors."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.wsproto import card

from . import corpus

V = corpus.S2["card"]
WS = corpus.IDS["workspace_a"]


@pytest.mark.parametrize("case", V["cases"], ids=[c["name"] for c in V["cases"]])
def test_card_cases(case):
    pk = bytes.fromhex(case["owner_pk_pub"])
    want = case["expect"]
    if "ok" in want:
        assert card.accept_card(case["stored"], case["card"], pk) == want["ok"]
    else:
        with pytest.raises(card.CardRefused) as e:
            card.accept_card(case["stored"], case["card"], pk)
        assert e.value.code == want["refuse"]


def test_verify_card_on_the_vector_card():
    o = card.verify_card(V["card"], corpus.pub("person_alice.sig"))
    assert o == V["card"]["o"]
    assert card.verify_card(V["card_seq_2"], corpus.pub("person_alice.sig"))["card_seq"] == 2


def test_wrong_suite_card():
    c = {**V["card"], "o": {**V["card"]["o"], "suite": 1}}
    with pytest.raises(card.CardRefused) as e:
        card.verify_card(c, corpus.pub("person_alice.sig"))
    assert e.value.code == "wrong_suite"


@pytest.mark.parametrize("mutate", ["extra_top", "missing_sealed", "not_a_dict", "bad_sealed", "seq_zero"])
def test_malformed_cards(mutate):
    c = dict(V["card"])
    if mutate == "extra_top":
        c["x"] = 1
    elif mutate == "missing_sealed":
        del c["sealed"]
    elif mutate == "not_a_dict":
        c = []
    elif mutate == "bad_sealed":
        c["sealed"] = "+/"
    elif mutate == "seq_zero":
        c["o"] = {**c["o"], "card_seq": 0}
    with pytest.raises(card.CardRefused) as e:
        card.verify_card(c, corpus.pub("person_alice.sig"))
    assert e.value.code == "malformed"


def test_sealed_part_vectors():
    ck = bytes.fromhex(V["card_key"])
    assert card.card_key(ck, WS, 1) == bytes.fromhex(V["k_card"])
    assert card.card_aad(WS, 1) == bytes.fromhex(V["aad"])
    sealed = card.seal_card_part(ck, WS, 1, V["sealed_part_plaintext"])
    assert sealed == bytes.fromhex(V["sealed_part"])
    assert card.open_card_part(ck, WS, 1, sealed) == V["sealed_part_plaintext"]
    assert card.ck_commit(WS, 1, ck) == V["ck_commit"]
    assert card.sealed_hash(sealed) == V["card"]["o"]["sealed_hash"]
    assert crypto.unb64u(V["card"]["sealed"]) == sealed


def test_open_card_part_refuses_another_seq_or_key():
    ck = bytes.fromhex(V["card_key"])
    sealed = bytes.fromhex(V["sealed_part"])
    with pytest.raises(card.CardRefused):
        card.open_card_part(ck, WS, 2, sealed)
    with pytest.raises(card.CardRefused):
        card.open_card_part(bytes(32), WS, 1, sealed)


def test_card_signed_bytes_match_the_vector():
    assert card.signed_bytes(V["card"]["o"]) == bytes.fromhex(V["signed_bytes_wsk"])
    assert canon.dumps(V["card"]["o"]) == bytes.fromhex(V["signed_bytes_wsk"])[len(b"orch/v2/sig/card-wsk|") :]


# --- the relay's code order for a malformed sealed_hash (review L5) ---------------------------------------------------

BAD_HASH = "!!not-b64u!!"


def _with_bad_hash(c):
    return {**c, "o": {**c["o"], "sealed_hash": BAD_HASH}}


def test_bad_sealed_hash_with_another_owner_is_other_person():
    with pytest.raises(card.CardRefused) as e:
        card.verify_card(_with_bad_hash(V["card"]), corpus.pub("person_bob.sig"))
    assert e.value.code == "other_person"


def test_bad_sealed_hash_with_a_mismatched_delegation_is_delegation_mismatch():
    case = next(c for c in V["cases"] if c["name"] == "delegation_for_another_wsk")
    with pytest.raises(card.CardRefused) as e:
        card.verify_card(_with_bad_hash(case["card"]), bytes.fromhex(case["owner_pk_pub"]))
    assert e.value.code == "delegation_mismatch"


def test_bad_sealed_hash_under_a_bad_signature_is_bad_signature():
    with pytest.raises(card.CardRefused) as e:  # the changed o no longer matches the signature
        card.verify_card(_with_bad_hash(V["card"]), corpus.pub("person_alice.sig"))
    assert e.value.code == "bad_signature"
