"""M3 secrets S-3: the WHOLE ``contracts/hub/vectors_org_secrets_bundle.json`` replayed with the host verifier.

Every case must give its expected answer; a required case name that is missing, or a file with fewer cases, fails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.hubclient import orgbundles as OB
from orch.hubclient import wire
from orch.hubclient.errors import HubRefused

VEC = json.loads((Path(__file__).resolve().parents[4] / "contracts/hub/vectors_org_secrets_bundle.json").read_text())
HELD = wire.verify_card(VEC["card"], now_ms=VEC["now_ms"], dev_mode=True)
REQUIRED = {
    "accepted",
    "the same version with other bytes is a conflict",
    "a lower version is a rollback",
    "the bundle of another workspace",
    "signed by the wrong key",
    "a skills bundle is not a secrets bundle",
    "a plaintext member smuggled into an entry",
    "a wrap that is too short",
    "a wrap that does not start with a curve point",
    "a wrap that is not base64url",
    "secrets not sorted by name",
    "a duplicate name",
}


def run(case: dict) -> dict:
    a = case["args"]
    try:
        OB.verify_org_secrets(a["bundle"], HELD, workspace_id=a["workspace_id"], dev_mode=True, pinned=a["pinned"])
        return {"ok": True}
    except HubRefused as e:
        return {"error": e.code}


@pytest.mark.parametrize("case", VEC["cases"], ids=[c["name"] for c in VEC["cases"]])
def test_case(case: dict) -> None:
    assert run(case) == case["expect"]


def test_nothing_is_skipped() -> None:
    names = {c["name"] for c in VEC["cases"]}
    assert REQUIRED <= names and len(VEC["cases"]) >= 20 and len(names) == len(VEC["cases"])
    got = {run(c).get("error") for c in VEC["cases"]}
    assert got >= {None, "bundle_conflict", "stale_bundle", "other_workspace", "bad_signature", "malformed"}
    assert {k for k in VEC} == {"card", "cases", "doc", "now_ms"}  # a new top-level member is a new thing to replay
