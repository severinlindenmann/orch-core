"""M3 secrets S-2: the committed ``contracts/hub/vectors_org_secrets_bundle.json`` is what the generator builds, and every case
gives its expected answer under ``verify_org_secrets`` (the host replays the same file in S-3)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from orch_hub.contract import hub as H

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub"
DOC = json.loads((VEC / "vectors_org_secrets_bundle.json").read_text())


def test_file_is_generated() -> None:
    spec = importlib.util.spec_from_file_location(
        "make_vectors_org_secrets_bundle", VEC / "make_vectors_org_secrets_bundle.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert json.loads(json.dumps(mod.build())) == DOC


def test_every_case_holds() -> None:
    held = H.verify_card(DOC["card"], now_ms=DOC["now_ms"], dev_mode=True)
    codes = set()
    for c in DOC["cases"]:
        a = c["args"]
        try:
            H.verify_org_secrets(a["bundle"], held, workspace_id=a["workspace_id"], dev_mode=True, pinned=a["pinned"])
            got = {"ok": True}
        except H.Refused as e:
            got = {"error": e.code}
        assert got == c["expect"], c["name"]
        codes.add(got.get("error"))
    assert codes >= {None, "bundle_conflict", "stale_bundle", "other_workspace", "bad_signature", "malformed"}
    assert len(DOC["cases"]) >= 20
