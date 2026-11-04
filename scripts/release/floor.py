#!/usr/bin/env python3
"""Rewrite the revocations floor baked into install.sh (the oldest revocation list a FIRST install accepts).

    floor.py install.sh release/keys/revocations.json     # bump the floor to that root-signed list (never lowers it)
    floor.py --check install.sh release/keys/revocations.json   # exit 1 if the floor is older than the list

``revocations.json`` is the root-signed object ``{"doc": <b64 bytes>, "root_key_id", "sig"}``; only its public ``doc``
is read here (its signature is checked by every verifier, not by this script). Run it for every release, before
install.sh is published as a release asset.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

ISSUED = re.compile(r'^REVOCATIONS_FLOOR_ISSUED_AT = "(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"$', re.M)
SERIAL = re.compile(r"^REVOCATIONS_FLOOR_MIN_SERIAL = (\d+)$", re.M)


def read_floor(text: str) -> tuple[str, int]:
    a, b = ISSUED.search(text), SERIAL.search(text)
    if a is None or b is None:
        raise ValueError("install.sh has no revocations floor lines")
    return a[1], int(b[1])


def read_list(path: Path) -> tuple[str, int]:
    doc = json.loads(base64.b64decode(json.loads(path.read_text(encoding="utf-8"))["doc"], validate=True))
    return doc["issued_at"], doc["min_serial"]


def bumped(text: str, issued_at: str, min_serial: int) -> str:
    """``text`` with the floor raised to (issued_at, min_serial); each value only ever goes up."""
    cur_at, cur_serial = read_floor(text)
    new_at, new_serial = max(cur_at, issued_at), max(cur_serial, min_serial)  # fixed-width UTC stamps sort as text
    text = ISSUED.sub(f'REVOCATIONS_FLOOR_ISSUED_AT = "{new_at}"', text, count=1)
    return SERIAL.sub(f"REVOCATIONS_FLOOR_MIN_SERIAL = {new_serial}", text, count=1)


def is_stale(text: str, issued_at: str, min_serial: int) -> bool:
    cur_at, cur_serial = read_floor(text)
    return cur_at < issued_at or cur_serial < min_serial


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    if len(args) != 2:
        sys.stderr.write(__doc__ or "")
        return 2
    sh, lst = Path(args[0]), Path(args[1])
    text = sh.read_text(encoding="utf-8")
    issued_at, min_serial = read_list(lst)
    if check:
        if is_stale(text, issued_at, min_serial):
            sys.stderr.write("install.sh's revocations floor is older than the current revocation list: run floor.py\n")
            return 1
        return 0
    sh.write_text(bumped(text, issued_at, min_serial), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
