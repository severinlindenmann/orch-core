"""Opus review of orch-hub#34 (C3b contract addendum), l2-add. Each test names a finding in
state/l2-review-p4.md "Addendum hub#34". They FAIL on f10a457 by design and must pass after the fix.

L2-ADD-1: the contract's admission check (§8.3 step 7, reference ``verify_custodial(live_items={item_hash: log})``)
binds the event only to "some live item of this ticket". Core §15.7 step 2 also requires that the decision's target
(``f1.gates[gate]`` / ``f1.questions[q]``) is in the item the person was shown, and step 3 the chain head. The
contract and its vectors carry neither, so an implementation that passes every vector admits a decision on a gate
the owner never shared in that item."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from orch_hub.contract import cases as C
from orch_hub.contract import phase4 as P4
from orch_hub.contract import vectorgen as VG

V = json.loads((Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub.json").read_text())
NAMES = {c["name"]: c for c in V["cases"]}
F1_ITEM = V["phase4"]["share_item_f1"]["item"]
F1_HASH = V["phase4"]["share_item_f1"]["item_hash"]


def _admit(event: dict) -> dict:
    a = copy.deepcopy(NAMES["custodial_selective_granted_member_decides"]["args"])
    a["event"] = event
    a["live_items"] = {F1_HASH: F1_ITEM["o"]["chain"]["log"]}
    try:
        return C.run("custodial", a)
    except P4.Refused as r:  # pragma: no cover - the fixed contract refuses
        return {"error": r.code}


def test_preconditions():
    # the real f1 item of the vectors: a ticket item of TKT1 whose f1 names code, plan, verify and Q1 only
    assert F1_ITEM["o"]["chain"]["log"] == VG.TICKET
    assert set(F1_ITEM["o"]["f1"]["gates"]) == {"code", "plan", "verify"}
    assert P4.item_hash(F1_ITEM) == F1_HASH


def test_l2_add_1_gate_not_in_item_f1_is_refused_at_admission():
    # gate.approved on "requirements": the item the person was shown has no requirements gate in f1.
    # build_selective refuses this (unknown_item), but the host's admission check must too (core §15.7 step 2):
    # the hub is not trusted to have run build_selective.
    ev = VG.custodial(item_hash=F1_HASH)
    assert ev["type"] == "gate.approved" and ev["gate"] == "requirements"
    out = _admit(ev)
    assert out.get("error") == "not_granted", f"admitted a decision on a gate outside the shared item: {out}"


def test_l2_add_1_question_item_never_admits_a_gate_decision():
    # same item hash, but the event is an answer to a question the item does not carry
    ev = VG.custodial(
        typ="question.answered",
        item_hash=F1_HASH,
        payload={"question": "Q7", "hash": "sha256:" + "ab" * 32, "text": "yes"},
    )
    out = _admit(ev)
    assert out.get("error") == "not_granted", f"admitted an answer to a question outside the shared item: {out}"


def test_l2_add_1_contract_input_cannot_express_the_check():
    # the reference input is {item_hash: log}: no f1, no chain. The fix gives the admission check the item itself
    # (or {item_hash: {log, f1, chain}}), as core §15.7 already specifies.
    import inspect

    from orch_hub.contract import hub as H

    src = inspect.getsource(H.verify_custodial)
    assert "f1" in src, "verify_custodial never looks at the item's f1 block"


@pytest.mark.parametrize("attempt", [1, 5, 10, 30, 10_000])
def test_deliver_backoff_bounded(attempt):
    # §8.7 (checked, no finding): the wait never exceeds 5 min and never goes below 1 s
    w = P4.deliver_next(None, attempt=attempt, first_sent_ms=0, now_ms=1)["resend_in_ms"]
    assert 1_000 <= w <= 300_000


def test_deliver_gives_up_after_24h_with_last_code():
    assert P4.deliver_next({"error": "unknown_snapshot"}, attempt=3, first_sent_ms=0, now_ms=86_400_000) == {
        "drop": "unknown_snapshot"
    }


def test_foreign_user_unaliased_first_record_cannot_be_claimed_by_alias():
    # §9.1 (checked): an id first recorded unaliased under hub A stays A's even if hub B's book aliases it
    with pytest.raises(P4.Refused) as r:
        P4.check_user_hub({"u1": "A"}, {"B": {"u1": "p1"}}, "B", "u1")
    assert r.value.code == "foreign_user"
