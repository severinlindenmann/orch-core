"""The orch.hub-policy/1 ``policy`` vectors (orch-hub ``vectors_hub_policy.json``, vendored) against core's validator
``orch.hubclient.marketpolicy`` (D14, orch-core#927). The ``evaluate`` cases are the market package's (G7, #928)."""

from __future__ import annotations

import json

import pytest

from orch.hubclient import HubRefused
from orch.hubclient import marketpolicy as MP
from orch.market.constants import OFFICIAL_URL

from .vec import CONTRACT_DIR

DOC = json.loads((CONTRACT_DIR / "vectors_hub_policy.json").read_text())
POLICY = [c for c in DOC["cases"] if c["check"] == "policy"]


def test_the_vector_file_states_core_capability_list():
    from orch.addons.manifest import CAPABILITIES

    assert sorted(DOC["capabilities"]) == sorted(CAPABILITIES) and len(DOC["capabilities"]) == 7


def test_file_shape_and_constants_match_core():
    assert DOC["official_url"] == OFFICIAL_URL
    assert len(POLICY) >= 50 and any(c["check"] == "evaluate" for c in DOC["cases"])
    assert DOC["default_policy"] == next(c for c in POLICY if c["name"] == "policy_default")["args"]["policy"]


@pytest.mark.parametrize("case", POLICY, ids=[c["name"] for c in POLICY])
def test_policy_case(case):
    p = case["args"]["policy"]
    if "error" in case["expect"]:
        with pytest.raises(HubRefused) as e:
            MP.parse_policy(p)
        assert e.value.code == case["expect"]["error"]
    else:
        MP.parse_policy(p)
        assert MP.policy_hash(p) == case["expect"]["policy_hash"]
