#!/usr/bin/env python3
"""The seq of the currently published channel.json, but only after it VERIFIES: ``channel_seq.py FILE --channel C
--revocations R`` prints the seq. An unsigned, badly signed or otherwise unverifiable file exits 1 (never trusted).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh import Refused, core_verifier, verify_published  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--revocations", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        doc = verify_published(
            a.file.read_bytes(),
            a.channel,
            json.loads(a.revocations.read_text(encoding="utf-8")),
            core_verifier(),
        )
    except (Refused, OSError, ValueError) as exc:
        print(f"channel_seq: REFUSED: {exc}", file=sys.stderr)
        return 1
    print(doc["seq"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
