"""G7 (#928): the hub marketplace policy decision, against the shared ``vectors_hub_policy.json`` ``evaluate`` cases
(hub and core run the same file). The product (``orch.market.policy.evaluate``) and the independent reference
(``tests/addons/market_ref.evaluate``) both give the vector's answer, plus the core-only rows: the run-time re-check
also applies ``allow`` and the company publisher key (L2 of the D14 review), and the stage split G3 calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.market import MarketError, policy
from tests.addons import market_ref as ref

VEC = json.loads((Path(__file__).parents[1] / "hub/vectors/vectors_hub_policy.json").read_text())
CASES = [c for c in VEC["cases"] if c["check"] == "evaluate"]
OFFICIAL = VEC["official_url"]


def product(args: dict) -> dict:
    r = args["req"]
    try:
        pol = policy.from_raw(args["policy"])
        if r["stage"] == "run":  # a marketplace add-on's install record holds the key it was installed under
            e = pol.market(r["market_url"])
            policy.check_run(
                pol, name=r["id"], version=r["version"], capabilities=r["capabilities"],
                origin=r["market_url"] or None, key=e.keys[0] if e is not None and e.keys else None,
            )  # fmt: skip
        else:
            policy.evaluate(r["stage"], pol, {**r, "url": r["market_url"]})
        return {"ok": True}
    except MarketError as exc:
        return {"error": exc.code}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_product_gives_the_shared_answer(case):
    assert product(case["args"]) == case["expect"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_reference_gives_the_shared_answer(case):
    want = ref.run_case(case)
    got = {"ok": True} if "ok" in want else {"error": want["refuse"]}
    assert got == case["expect"]


def test_the_vectors_are_the_ones_the_hub_shipped():
    assert len(CASES) == 35 and OFFICIAL == "https://github.com/severinlindenmann/orch-marketplace"


# ------------------------------------------------------------------------------------------------ core-only rows
def _pol(markets=None, allow=None, block=(), ceiling=None):
    return policy.from_raw(
        {
            "markets": markets if markets is not None else [{"url": OFFICIAL, "class": "official", "keys": []}],
            "addons": {"allow": allow, "block": list(block)},
            "ceiling": ceiling,
        }
    )


K1, K2 = VEC["keys"]["company_1"], VEC["keys"]["company_2"]
ACME = "https://git.acme.example/tools/orch-market"
COMPANY = [
    {"url": ACME, "class": "company", "keys": sorted([K1, K2])},
    {"url": OFFICIAL, "class": "official", "keys": []},
]


def run(pol, **kw):
    base = {"url": ACME, "id": "acme-tool", "version": "1.0.0", "capabilities": [], "publisher_key": K1}
    policy.evaluate("run", pol, {**base, **kw})


def test_L2_run_applies_allow_to_a_marketplace_addon_but_not_to_a_builtin():
    with pytest.raises(MarketError) as e:
        run(_pol(COMPANY, allow=["other"]))
    assert e.value.code == "market.addon_not_allowed"
    run(_pol(COMPANY, allow=["acme-tool"]))
    run(_pol(COMPANY, allow=["other"]), url="", publisher_key=None, id="dashboard")  # built-in: block and ceiling only


def test_L2_run_applies_the_company_publisher_pin():
    run(_pol(COMPANY))
    with pytest.raises(MarketError) as e:
        run(_pol(COMPANY), publisher_key=VEC["keys"]["attacker"])  # a key the hub no longer pins
    assert e.value.code == "market.publisher_unpinned"


def test_check_run_refuses_a_company_addon_whose_record_holds_no_key():
    pol = _pol(COMPANY)
    with pytest.raises(MarketError) as e:
        policy.check_run(pol, name="acme-tool", version="1.0.0", capabilities=[], origin=ACME, key=None)
    assert e.value.code == "market.publisher_unpinned"
    policy.check_run(pol, name="acme-tool", version="1.0.0", capabilities=[], origin=ACME, key=K1)


def test_a_standalone_host_is_never_restricted():
    for stage in policy.STAGES:
        policy.evaluate(stage, None, {"url": "x", "id": "a", "version": "1.0.0", "capabilities": ["network"]})
    policy.check_run(None, name="a", version="1.0.0", capabilities=["pty"], origin=ACME, key=None)


def test_an_unknown_stage_is_a_bug_not_an_allow():
    with pytest.raises(ValueError):
        policy.evaluate("later", _pol(), {})


def test_from_raw_never_reads_unreadable_or_malformed_as_standalone_or_empty():
    for raw in (None, {}, {"markets": [], "addons": {"allow": None, "block": []}, "ceiling": None, "x": 1}):
        with pytest.raises(MarketError) as e:
            policy.from_raw(raw)
        assert e.value.code == "market.not_allowed"


def test_the_stages_split_in_the_spec_order():
    pol = _pol(COMPANY, allow=["x"], block=["acme-tool"], ceiling=[])
    req = {
        "url": ACME, "id": "acme-tool", "version": "1.0.0", "capabilities": ["pty"], "index_key": K1,
        "publisher_key": "z",
    }  # fmt: skip
    for stage, code in (
        ("index", None),  # index key pinned
        ("entry", "market.blocked"),
        ("publisher", "market.publisher_unpinned"),
        ("manifest", "market.capability_ceiling"),
        ("grant", "market.blocked"),
        ("install", "market.blocked"),
    ):
        if code is None:
            policy.evaluate(stage, pol, req)
            continue
        with pytest.raises(MarketError) as e:
            policy.evaluate(stage, pol, req)
        assert e.value.code == code


def test_gate_gives_the_class_and_needs_no_isolation_for_a_listed_company_marketplace():
    pol = _pol(COMPANY)
    assert policy.gate(OFFICIAL, policy=pol) == "official"
    assert policy.gate(ACME, policy=pol) == "company"
    with pytest.raises(MarketError) as e:
        policy.gate("https://git.acme.example/other/market", policy=pol)
    assert e.value.code == "market.not_allowed"
    with pytest.raises(MarketError) as e:
        policy.gate(OFFICIAL, policy=_pol([]))
    assert e.value.code == "market.not_allowed"
    with pytest.raises(MarketError) as e:
        policy.gate(ACME, policy=None)  # standalone: a community marketplace stays unisolated
    assert e.value.code == "market.community_unisolated"
    assert policy.gate(ACME, policy=None, dev_url=ACME) == "dev"
    assert policy.gate(ACME, policy=pol, dev_url=ACME) == "company"  # the hub's pin wins over the dev entry
