"""Builds ``contracts/hub/vectors_org_skills.json``: the signed ``hub.org_skills`` bundle (M3, #1430, Opus conditions 1 and 4).

Test code, deterministic (FAKE keys from fixed seeds). The hub replays every case against ``verify_org_skills``; the host
side (orch-core, the M3 host adoption slice) must replay the same file and give the same answers.
``uv run python contracts/hub/make_vectors_org_skills.py`` rewrites the file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orch_hub.contract import vectorgen as G
from orch_hub.contract.org_bundles import skill_digest

RLO = chr(0x202E)  # built, so no bidi character sits in this file
TEXT = "Write invoice numbers as INV-2026-0001."


def skill(name: str, text: str = TEXT, **kw: Any) -> dict[str, Any]:
    return {"name": name, "text": text, "digest": skill_digest(name, text), **kw}


def bundle(version: int = 2, skills: list | None = None, ws: str = G.WS_A, key: str = "hk_op", **kw: Any) -> dict[str, Any]:
    o = {
        **G.hdr("hub.org_skills"),
        "hub_id": G.HUB_ID,
        "op_cert_id": G.OP_CERT_ID,
        "workspace_id": ws,
        "version": version,
        "issued_ms": G.NOW - 60_000,
        "skills": skills if skills is not None else [skill("invoices")],
        "dev": True,
        **kw,
    }
    return G.signed(key, "sig_hub_org_skills", o)


def build() -> dict[str, Any]:
    ok = bundle()
    cases = [
        ("accepted", {"bundle": ok, "workspace_id": G.WS_A, "pinned": None}, {"ok": True}),
        ("empty bundle (everything withdrawn) is valid", {"bundle": bundle(3, []), "workspace_id": G.WS_A, "pinned": None}, {"ok": True}),
        ("a higher version replaces the pinned one", {"bundle": ok, "workspace_id": G.WS_A, "pinned": bundle(1)["o"]}, {"ok": True}),
        ("the same version and bytes is a no-op", {"bundle": ok, "workspace_id": G.WS_A, "pinned": ok["o"]}, {"ok": True}),
        ("the same version with other bytes is a conflict", {"bundle": ok, "workspace_id": G.WS_A, "pinned": bundle(2, [skill("support", "Greet people.")])["o"]}, {"error": "bundle_conflict"}),
        ("a lower version is a rollback", {"bundle": bundle(1), "workspace_id": G.WS_A, "pinned": ok["o"]}, {"error": "stale_bundle"}),
        ("the bundle of another workspace", {"bundle": bundle(ws=G.WS_B), "workspace_id": G.WS_A, "pinned": None}, {"error": "other_workspace"}),
        ("signed by the wrong key", {"bundle": bundle(key="hk_root"), "workspace_id": G.WS_A, "pinned": None}, {"error": "bad_signature"}),
        ("content changed after signing", {"bundle": {"o": {**ok["o"], "skills": []}, "sig": ok["sig"]}, "workspace_id": G.WS_A, "pinned": None}, {"error": "bad_signature"}),
        ("a name starting with orch shadows a builtin skill", {"bundle": bundle(skills=[skill("orch-tickets")]), "workspace_id": G.WS_A, "pinned": None}, {"error": "malformed"}),
        ("a bidi control character in the text", {"bundle": bundle(skills=[skill("invoices", "ok" + RLO)]), "workspace_id": G.WS_A, "pinned": None}, {"error": "malformed"}),
        ("front matter", {"bundle": bundle(skills=[skill("invoices", "---\nname: x\n---\nbody")]), "workspace_id": G.WS_A, "pinned": None}, {"error": "malformed"}),
        ("a text over 2400 characters", {"bundle": bundle(skills=[skill("invoices", "x" * 2401)]), "workspace_id": G.WS_A, "pinned": None}, {"error": "malformed"}),
        ("a digest that does not match the text", {"bundle": bundle(skills=[skill("invoices", digest="sha256:" + "0" * 64)]), "workspace_id": G.WS_A, "pinned": None}, {"error": "malformed"}),
        ("skills not sorted by name", {"bundle": bundle(skills=[skill("support"), skill("invoices")]), "workspace_id": G.WS_A, "pinned": None}, {"error": "malformed"}),
    ]
    return {
        "doc": "hub.org_skills (M3 #1430): replay each case with verify_org_skills against the card",
        "card": G.card(),
        "now_ms": G.NOW,
        "cases": [{"name": n, "args": a, "expect": e} for n, a, e in cases],
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_org_skills.json")
    out.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {out}\n")
