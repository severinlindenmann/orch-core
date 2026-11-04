"""M3-3: the committed ``contracts/hub/vectors_org_skills.json`` is what the generator builds, and every case gives its
expected answer under ``verify_org_skills`` (the host side replays the same file)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from orch_hub.contract import hub as H

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub"


def _gen():
    spec = importlib.util.spec_from_file_location("make_vectors_org_skills", VEC / "make_vectors_org_skills.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_file_is_generated() -> None:
    assert json.loads((VEC / "vectors_org_skills.json").read_text()) == json.loads(json.dumps(_gen().build()))


def test_cases_hold() -> None:
    doc = json.loads((VEC / "vectors_org_skills.json").read_text())
    held = H.verify_card(doc["card"], now_ms=doc["now_ms"], dev_mode=True)
    codes = set()
    for c in doc["cases"]:
        a = c["args"]
        try:
            H.verify_org_skills(a["bundle"], held, workspace_id=a["workspace_id"], dev_mode=True, pinned=a["pinned"])
            got = {"ok": True}
        except H.Refused as e:
            got = {"error": e.code}
        assert got == c["expect"], c["name"]
        codes.add(got.get("error"))
    assert codes >= {None, "bundle_conflict", "stale_bundle", "other_workspace", "bad_signature", "malformed"}


def test_text_cases_hold() -> None:
    """The host replays these same cases with its own copy of the rule (orch-core ``orch.hubclient.orgtext``)."""
    from orch_hub.contract.org_bundles import skill_problem

    doc = json.loads((VEC / "vectors_org_skills.json").read_text())
    for c in doc["text_cases"]:
        assert skill_problem(c["skill_name"], c["text"]) == c["expect"], c["name"]
    assert {"variation selector", "tag character", "hangul filler", "line separator"} <= {
        c["name"] for c in doc["text_cases"]
    }


def test_mcp_cases_hold() -> None:
    """Server entries and bundles; the host replays the entries with its own ``mcp_problem`` (orch-core ``orgtext``)."""
    from orch_hub.contract.org_bundles import mcp_problem

    doc = json.loads((VEC / "vectors_org_skills.json").read_text())
    for c in doc["mcp_cases"]:
        assert mcp_problem(c["entry"]) == c["expect"], c["name"]
    held = H.verify_card(doc["card"], now_ms=doc["now_ms"], dev_mode=True)
    for c in doc["mcp_bundle_cases"]:
        a = c["args"]
        try:
            H.verify_org_mcp(a["bundle"], held, workspace_id=a["workspace_id"], dev_mode=True, pinned=a["pinned"])
            got = {"ok": True}
        except H.Refused as e:
            got = {"error": e.code}
        assert got == c["expect"], c["name"]
