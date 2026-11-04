"""The hub's real ``space_list`` reply satisfies the shared vectors: meta only names listed spaces, values are exactly
name and kind (short printable strings)."""

import json
from pathlib import Path

CASES = json.loads((Path(__file__).resolve().parents[3] / "contracts/hub/vectors_space_list.json").read_text())["cases"]


def shape_ok(b):
    """The shared rule, written independently of core's code."""
    if set(b) != {"spaces", "meta"}:
        return False
    ids = {r["o"]["space_id"] for r in b["spaces"]}
    caps = {"name": 64, "kind": 32}
    return all(
        sid in ids
        and set(m) == set(caps)
        and all(type(m[k]) is str and len(m[k]) <= cap and m[k].isprintable() for k, cap in caps.items())
        for sid, m in b["meta"].items()
    )


def test_the_shape_rule_agrees_with_every_shared_vector():
    assert [shape_ok(c["reply"]) for c in CASES] == [c["expect"] == "ok" for c in CASES]
