#!/usr/bin/env python3
"""Re-time a published channel manifest for the weekly refresh.

    refresh.py --envelope channel.json --channel develop --issued-at T --expires-at T --out payload.json

Reads the current signed envelope, takes its manifest payload, changes only issued_at and expires_at, and writes the
canonical bytes for sign.py. Version, seq, commit and files stay the same, so nothing is rebuilt and ``seq`` does not
move (equal seq is accepted by verifiers). The old signature is not trusted here; the result is signed afresh.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def jcs(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def refresh(envelope: dict, channel: str, issued_at: str, expires_at: str) -> bytes:
    for label, value in (("issued_at", issued_at), ("expires_at", expires_at)):
        if TIME.fullmatch(value) is None:
            raise ValueError(f"bad {label}")
    if not issued_at < expires_at:
        raise ValueError("expires_at must be after issued_at")
    if envelope.get("format") != "orch.release-envelope/1":
        raise ValueError("not a release envelope")
    doc = json.loads(base64.b64decode(envelope["payload"], validate=True))
    if doc.get("format") != "orch.release/1" or doc.get("channel") != channel:
        raise ValueError("the published manifest is not a core manifest for this channel")
    doc["issued_at"] = issued_at
    doc["expires_at"] = expires_at
    return jcs(doc)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--envelope", type=Path, required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--issued-at", required=True)
    ap.add_argument("--expires-at", required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        out = refresh(json.loads(a.envelope.read_text(encoding="utf-8")), a.channel, a.issued_at, a.expires_at)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"refresh: {exc}", file=sys.stderr)
        return 1
    a.out.write_bytes(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
