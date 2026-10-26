"""The hub labels are new, end in "|" and keep the union with every orch v2 label prefix-free (protocol-v2 §3)."""

from __future__ import annotations

import itertools

import pytest

from orch_hub.contract.labels import CUSTODIAL_PREFIX, HUB_LABELS, V2_REUSED, custodial_label

# Frozen copy of the v2 "|"-terminated labels (orch-core orch.crypto.labels LABELS + PLANNED_LABELS and
# orch.canon.LABELS, origin/develop of 22 Oct 2026). The cross-check test reads the live table when orch-core is present.
V2 = [
    "orch/v2/seal-aad|",
    "orch/v2/push-aad|",
    "orch/v2/wrap-aad|",
    "orch/v2/card-aad|",
    "orch/v2/label-aad|",
    "orch/v2/drop-meta-aad|",
    "orch/v2/sig/device-cert|",
    "orch/v2/sig/revocation|",
    "orch/v2/sig/card-wsk|",
    "orch/v2/sig/ws-delegation|",
    "orch/v2/sig/cert-challenge|",
    "orch/v2/sig/push|",
    "orch/v2/sig/decision|",
    "orch/v2/sig/member-list|",
    "orch/v2/sig/wk-grant|",
    "orch/v2/sig/sk-grant|",
    "orch/v2/sig/bridge|",
    "orch/v2/sig/cert-request|",
    "orch/v2/sig/genesis-pin|",
    "orch/v2/sig/cert-rejection|",
    "orch/v2/sig/enroll-request|",
    "orch/v2/sig/drop-object|",
    "orch/v2/sig/drop-claim|",
    "orch/v2/ws-envelope|",
    "orch/v2/sig/ws-cosign|",
    "orch/v2/sig/webauthn-bind|",
    "orch/v2/sig/relay-auth|",
    "orch/v2/publish|",
    "orch/v2/id/device|",
    "orch/v2/id/person|",
    "orch/v2/pin/person|",
    "orch/v2/pin/workspace|",
    "orch/v2/sas|",
    "orch/v2/sas-cert|",
    "orch/v2/dk-hash|",
    "orch/v2/wk-commit|",
    "orch/v2/event|",
    "orch/v2/gate|",
    "orch/v2/grant-secret|",
    "orch/v2/people|",
    "orch/v2/policy|",
    "orch/v2/question-id|",
    "orch/v2/section|",
    "orch/v2/value|",
    "orch/v2/dek-commit|",
    "orch/v2/card-key-commit|",
    "orch/v2/drop-parent|",
    "orch/v2/ws-envelope-hash|",
    "orch/v2/question|",
    "orch/v2/assert|",
    "orch/v2/webauthn-reg|",
    "orch/v2/enroll|",
    "orch/v2/host-drop-recovery|",
    "orch/v2/sig/drop-handoff|",
    "orch/v2/checkpoint-hash|",
    "orch/v2/log-acc|",
    "orch/v2/sig/ticket-event|",
    "orch/v2/sig/ws-event|",
    "orch/v2/sig/host-event|",
    "orch/v2/sig/checkpoint|",
    "orch/v2/sig/reader-decl|",
]

SAMPLE_CUSTODIAL = [custodial_label(t).decode() for t in ("gate.approved", "gate.approved_x", "question.answered")]


def test_hub_labels_are_new_and_terminated():
    assert all(v.endswith("|") and v.startswith("orch/v2/") for v in HUB_LABELS.values())
    assert not set(HUB_LABELS.values()) & set(V2)
    assert set(V2_REUSED.values()) <= set(V2)


def test_union_is_prefix_free():
    labels = sorted(set(V2) | set(HUB_LABELS.values()) | set(SAMPLE_CUSTODIAL))
    for a, b in itertools.permutations(labels, 2):
        assert not b.startswith(a), (a, b)


def test_custodial_family_is_disjoint_from_every_fixed_label():
    for lab in [*V2, *HUB_LABELS.values()]:
        assert not lab.startswith(CUSTODIAL_PREFIX) and not CUSTODIAL_PREFIX.startswith(lab)


@pytest.mark.parametrize("bad", ["gate", "Gate.approved", "gate.approved|x", "gate..x", "", "a.b.c.d.e", 3, None])
def test_custodial_label_grammar(bad):
    with pytest.raises(ValueError):
        custodial_label(bad)
