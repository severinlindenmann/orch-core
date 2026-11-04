"""Generate offset-append operation and raw-route vectors (no private keys needed)."""

import hashlib
import json
from pathlib import Path


def vectors():
    def ok(n, done=False, result="stored"):
        return {"status": 200, "body": {"received": n, "done": done, "result": result}}

    def error(code, status=409, **extra):
        return {"status": status, "body": {"error": code, **extra}}

    def part(offset, body, expect):
        return {"op": "part", "offset": offset, "body_hex": body.encode().hex(), "expect": expect}

    first = part(0, "ab", ok(2))
    cases = [
        (
            "three_parts",
            [
                first,
                part(2, "cd", ok(4)),
                part(4, "ef", ok(6, True)),
                part(4, "ef", ok(6, True, "unchanged")),
            ],
        ),
        ("wrong_whole_hash", [first, part(2, "cdXX", error("content_mismatch", 422, received=0))]),
        ("identical_retry", [first, part(0, "ab", ok(2, result="unchanged"))]),
        ("conflicting_retry", [first, part(0, "XX", error("part_conflict", received=2))]),
        ("gap", [first, part(3, "d", error("part_out_of_order", received=2))]),
        ("overlap_refused", [first, part(1, "bc", error("part_out_of_order", received=2))]),
        ("beyond_size", [first, part(5, "ef", error("too_large", 413))]),
        (
            "after_commit",
            [
                part(0, "abcdef", ok(6, True)),
                {"op": "commit"},
                part(0, "ab", error("share_committed")),
            ],
        ),
        (
            "after_revoke",
            [first, {"op": "revoke"}, part(0, "ab", error("gone", 410, state="revoked"))],
        ),
    ]
    result = {
        "suite": 2,
        "cases": [
            {
                "name": name,
                "path": "f",
                "size": 6,
                "sha256": hashlib.sha256(b"abcdef").hexdigest(),
                "steps": steps,
            }
            for name, steps in cases
        ],
    }
    for path in ("docs/parts/0/f", "docs/at/12", "at/f"):
        result["cases"].append(
            {
                **result["cases"][0],
                "name": path,
                "path": path,
                "steps": [
                    {
                        "op": "file",
                        "body_hex": b"abcdef".hex(),
                        "expect": {"status": 200, "body": {"path": path, "result": "stored"}},
                    }
                ],
            }
        )
    base = "/v1/ws/" + "a" * 32 + "/shares/" + "b" * 32 + "/parts/"
    result["route_cases"] = [
        {"name": suffix, "target": base + suffix + "/f", "valid": valid}
        for suffix, valid in [
            ("0", True),
            ("01", False),
            ("0?x=1", False),
            ("%30", False),
            ("27262976", True),
            ("27262977", False),
            ("-1", False),
        ]
    ]
    return result


if __name__ == "__main__":
    dest = Path(__file__).resolve().parents[3] / "contracts/publish/publish_v2_parts.json"
    dest.write_text(json.dumps(vectors(), indent=2, sort_keys=True) + "\n")
