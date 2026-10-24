"""D14 (orch-core#927): the committed ``vectors/vectors_hub_policy.json`` is exactly what the reference generator
builds, and every case gives its expected result under the reference validator and evaluation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

VEC = Path(__file__).resolve().parents[1] / "vectors"


def _gen():
    spec = importlib.util.spec_from_file_location("make_vectors_hub_policy", VEC / "make_vectors_hub_policy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_file_is_generated() -> None:
    assert json.loads((VEC / "vectors_hub_policy.json").read_text()) == json.loads(json.dumps(_gen().build()))


def test_cases_hold() -> None:
    g = _gen()
    doc = json.loads((VEC / "vectors_hub_policy.json").read_text())
    codes = set()
    for c in doc["cases"]:
        if c["check"] == "policy":
            try:
                g.check_policy(c["args"]["policy"])
                got = {"ok": True, "policy_hash": g.policy_hash(c["args"]["policy"])}
            except g.Malformed:
                got = {"error": "malformed"}
        else:
            try:
                g.evaluate(c["args"]["policy"], c["args"]["req"])
                got = {"ok": True}
            except g.Refuse as e:
                got = {"error": e.code}
        assert got == c["expect"], c["name"]
        codes.add(got.get("error"))
    assert codes >= {
        "malformed",
        "market.not_allowed",
        "market.index_unsigned",
        "market.blocked",
        "market.addon_not_allowed",
        "market.publisher_unpinned",
        "market.capability_ceiling",
    }
