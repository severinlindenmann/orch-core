"""H-1 (M3 host, Opus condition 1): the host verifies company bundles exactly as the hub signs them. The WHOLE shared
``contracts/hub/vectors_org_skills.json`` is replayed here with the core verifier: the skills bundle cases, the
MCP bundle cases, the text rules and the server-entry rules. The hub replays the same file against its own code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.hubclient import orgbundles as OB
from orch.hubclient import wire
from orch.hubclient.errors import HubRefused
from orch.hubclient.orgtext import mcp_problem, skill_problem

VEC = json.loads((Path(__file__).resolve().parents[4] / "contracts/hub/vectors_org_skills.json").read_text())
HELD = wire.verify_card(VEC["card"], now_ms=VEC["now_ms"], dev_mode=True)


def run(fn, case: dict) -> dict:
    a = case["args"]
    try:
        fn(a["bundle"], HELD, workspace_id=a["workspace_id"], dev_mode=True, pinned=a["pinned"])
        return {"ok": True}
    except HubRefused as e:
        return {"error": e.code}


@pytest.mark.parametrize("case", VEC["cases"], ids=[c["name"] for c in VEC["cases"]])
def test_skills_bundle_case(case: dict) -> None:
    assert run(OB.verify_org_skills, case) == case["expect"]


@pytest.mark.parametrize("case", VEC["mcp_bundle_cases"], ids=[c["name"] for c in VEC["mcp_bundle_cases"]])
def test_mcp_bundle_case(case: dict) -> None:
    assert run(OB.verify_org_mcp, case) == case["expect"]


@pytest.mark.parametrize("case", VEC["text_cases"], ids=[c["name"] for c in VEC["text_cases"]])
def test_text_case(case: dict) -> None:
    assert skill_problem(case["skill_name"], case["text"]) == case["expect"]


@pytest.mark.parametrize("case", VEC["mcp_cases"], ids=[c["name"] for c in VEC["mcp_cases"]])
def test_server_entry_case(case: dict) -> None:
    assert mcp_problem(case["entry"]) == case["expect"]


def test_the_error_codes_of_the_file_are_all_exercised() -> None:
    """The file's refusals the host can give: every one shows up, so a new vector cannot go unreplayed."""
    expected = {c["expect"].get("error") for c in [*VEC["cases"], *VEC["mcp_bundle_cases"]]} - {None}
    got = set()
    for c in VEC["cases"]:
        got.add(run(OB.verify_org_skills, c).get("error"))
    for c in VEC["mcp_bundle_cases"]:
        got.add(run(OB.verify_org_mcp, c).get("error"))
    assert expected <= got
    assert {"bundle_conflict", "stale_bundle", "other_workspace", "bad_signature", "malformed"} <= expected


def test_every_case_of_every_list_is_replayed() -> None:
    """No list of the file is skipped (a new list added to the generator must be replayed here)."""
    assert {k for k, v in VEC.items() if isinstance(v, list)} == {
        "cases",
        "mcp_bundle_cases",
        "text_cases",
        "mcp_cases",
    }


def test_a_mutated_bundle_is_refused_in_each_member(tmp_path) -> None:
    """Beyond the vectors: changing one member of a verified bundle breaks it (the signature covers all)."""
    good = next(c for c in VEC["cases"] if c["name"] == "accepted")["args"]["bundle"]
    for key, val in (
        ("version", 9),
        ("workspace_id", "00" * 16),
        ("hub_id", "11" * 16),
        ("dev", False),
        ("skills", []),
    ):
        bad = {"o": {**good["o"], key: val}, "sig": good["sig"]}
        with pytest.raises(HubRefused):
            OB.verify_org_skills(bad, HELD, workspace_id=good["o"]["workspace_id"], dev_mode=True, pinned=None)
    with pytest.raises(HubRefused) as e:
        OB.verify_org_skills(good, HELD, workspace_id=good["o"]["workspace_id"], dev_mode=False, pinned=None)
    assert e.value.code == "dev_refused"
    with pytest.raises(HubRefused) as e:
        OB.verify_org_skills(
            good,
            HELD,
            workspace_id=good["o"]["workspace_id"],
            dev_mode=True,
            pinned=None,
            revoked_op_certs=frozenset({HELD["op_cert"]["cert_id"]}),
        )
    assert e.value.code == "revoked"
