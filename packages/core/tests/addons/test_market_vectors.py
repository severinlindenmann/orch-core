"""``orch.market/1``: the schema, the vectors and the error-code table of docs/architecture/orch-v2-marketplace.md.

The reference checker (``market_ref``) is test code. The product installer (G2/G3) must give the same answers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.addons import market_ref as ref

VECTORS = json.loads((Path(__file__).with_name("vectors_market.json")).read_text())
CASES = VECTORS["cases"]
DOC = Path(__file__).parents[4] / "docs/architecture/orch-v2-marketplace.md"

# codes the doc defines that need a network, git or a running host, so there is no pure vector for them here
NO_VECTOR = {"market.commit_mismatch", "market.fetch_failed", "market.staged_changed"}
# the hub-policy codes are covered by the shared vectors_hub_policy.json (`evaluate` cases, D14); see SHARED below
SHARED = json.loads((Path(__file__).resolve().parents[4] / "contracts/hub/vectors_hub_policy.json").read_text())["cases"]


def doc_codes() -> set[str]:
    text = DOC.read_text()
    table = text.split("## 13. Refusal codes", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `(market\.[a-z_]+)` \|", table, re.M))


def test_the_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(ref.schema())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_vector(case):
    assert ref.run_case(case) == case["expect"]


def test_vector_names_are_unique_and_the_set_is_not_one_sided():
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names))
    assert sum("ok" in c["expect"] for c in CASES) >= 14
    assert sum("refuse" in c["expect"] for c in CASES) >= 80


def test_every_code_in_the_doc_has_a_vector_and_every_vector_code_is_in_the_doc():
    codes = doc_codes()
    used = {c["expect"]["refuse"] for c in CASES if "refuse" in c["expect"]}
    used |= {c["expect"]["error"] for c in SHARED if c["check"] == "evaluate" and "error" in c["expect"]}
    assert used <= codes, f"vector codes missing from the doc: {sorted(used - codes)}"
    assert codes - used <= NO_VECTOR, f"doc codes without a vector: {sorted(codes - used - NO_VECTOR)}"


def test_the_spec_names_the_brief_negatives():
    """The cases G0 must cover, by name, so a refactor of the vectors cannot drop one silently."""
    names = {c["name"] for c in CASES}
    for n in (
        "index_unknown_entry_field",
        "index_bad_sha_short",
        "index_missing_pin",
        "install_digest_mismatch_changed_file",
        "install_min_orch_too_high",
        "index_duplicate_id",
        "install_publisher_key_changed",
        "install_community_without_isolation",
        "install_agent_refused",
        "install_lock_tampered_after_indexing",
        "install_signed_entry_replayed_into_another_marketplace",
        "install_older_signed_entry_than_seen",
        "lock_python_marker_smuggles_index_url",
        "lock_node_resolved_other_host",
        "index_entry_ip_literal_host",
        "install_entry_host_not_allowed",
        "address_refused_link_local_metadata",
    ):
        assert n in names


def test_a_valid_signature_over_the_entry_verifies_and_any_change_breaks_it():
    idx = next(c for c in CASES if c["name"] == "index_valid_full")["args"]["index"]
    entry = next(e for e in idx["addons"] if "publisher" in e)
    market = idx["source"]["git"]
    assert ref.verify_entry(entry, market)
    for field, value in (("version", "9.9.9"), ("commit", "f" * 40), ("package_sha256", "sha256:" + "0" * 64)):
        assert not ref.verify_entry({**entry, field: value}, market), field
    assert not ref.verify_entry(
        {**entry, "publisher": {**entry["publisher"], "key": VECTORS["keys"]["mallory"]}}, market
    )
    # the signature names the marketplace: the same entry does not verify anywhere else
    assert not ref.verify_entry(entry, "https://github.com/evil/mirror")


def test_the_vector_keys_are_distinct_and_no_private_key_is_in_the_file():
    assert len(set(VECTORS["keys"].values())) == 3
    assert "PRIVATE" not in json.dumps(VECTORS)
