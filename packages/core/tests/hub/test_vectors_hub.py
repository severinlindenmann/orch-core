"""The hub contract vectors (``contracts/hub/vectors_hub.json``) against core-side checks (V3-D8).

Every case of a check core or the fake hub runs must give exactly its ``expect``. Skipped on purpose (listed in
``vec.SKIPPED_CHECKS``): ``custodial`` (phase 4, C3) and ``publish`` (orch-publish verifies section 11.2).
"""

from __future__ import annotations

import pytest

from orch.canon.jcs import dumps as cj
from orch.crypto import suite as S
from orch.crypto.encoding import b64u, unb64u

from . import contract as H
from .vec import PENDING_VECTORS, RUN_CHECKS, SKIPPED_CHECKS, V, outcome, pub

CASES = [c for c in V["cases"] if c["check"] in RUN_CHECKS]


def test_the_contract_header_is_the_one_core_speaks():
    assert V["contract"] == "orch.hub/0" and V["hv"] == 0 and V["suite"] == 2


def test_every_check_is_either_run_or_skipped_with_a_reason():
    checks = {c["check"] for c in V["cases"]}
    assert checks == set(RUN_CHECKS) | set(SKIPPED_CHECKS)
    assert len(CASES) > 100  # the bulk of the corpus runs here


def _case_param(c):
    pending = c["name"] in PENDING_VECTORS
    marks = [pytest.mark.xfail(reason="core#1036: the hub contract is ahead of core", strict=False)] if pending else []
    return pytest.param(c, id=c["name"], marks=marks)


@pytest.mark.parametrize("case", [_case_param(c) for c in CASES])
def test_case(case):
    assert outcome(case) == case["expect"]


def test_positive_and_negative_cases_both_present():
    assert any("ok" in c["expect"] for c in CASES) and any("error" in c["expect"] for c in CASES)


def test_ids_and_labels():
    ids = V["ids"]
    assert ids["hub"] == H.hub_id(pub("hk_root"))
    assert ids["hub_rogue"] == H.hub_id(pub("hk_root_rogue"))
    assert ids["person_alice"] == H.person_id(pub("pk_alice"))
    assert ids["channel_alice_phone"] == H.channel_id(pub("channel_alice_phone"))
    for name, text in V["labels"].items():
        if name in H.HUB_LABELS:
            assert H.HUB_LABELS[name] == text


@pytest.mark.parametrize("e", V["signatures"], ids=[e["name"] for e in V["signatures"]])
def test_signatures_verify_under_core_suite(e):
    signed_hex = bytes.fromhex(e["signed_hex"])
    assert signed_hex == e["label"].encode() + bytes.fromhex(e["canonical_hex"])
    assert S.verify(pub(e["signer"]), unb64u(e["sig"], 64), signed_hex)


def test_canonical_json_matches_core_for_every_object():
    for e in V["signatures"]:
        o = _find(e["name"])
        if o is not None:
            assert cj(o) == bytes.fromhex(e["canonical_hex"])


def _find(name):
    objs = V["objects"]
    table = {
        "op_cert": objs["card"]["op_cert"]["o"],
        "card": objs["card"]["o"],
        "uk_cert": objs["uk_cert_alice"]["o"],
        "channel_cert": objs["channel_cert_alice_phone"]["o"],
        "snapshot": objs["snapshot_a_v3"]["o"],
        "binding": objs["binding_a_1"]["o"],
        "unbound": objs["unbound_a_1"]["o"],
        "op_revoked": objs["op_revoked"]["o"],
    }
    return table.get(name)


def test_link_auth_bytes_and_signature():
    a = V["link_auth"]
    msg = H.link_auth_bytes(
        a["hub_id"], a["workspace_id"], bytes.fromhex(a["challenge_hex"]), bytes.fromhex(a["exporter_hex"])
    )
    assert msg == bytes.fromhex(a["signed_hex"])
    assert S.verify(pub(a["signer"]), unb64u(a["sig"], 64), msg)
    assert b64u(unb64u(a["sig"])) == a["sig"]
