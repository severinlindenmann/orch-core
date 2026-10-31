#!/usr/bin/env python3
"""Normalize hash-locked requirement files into the release's ``constraints.txt`` (the manifest role ``constraints``).

    constraints.py --out constraints.txt resolved.txt [more.txt ...]

The input is ``uv pip compile --generate-hashes --universal`` over the release wheels. The output lists every
``name==version`` once, with the union of the ``--hash=sha256:`` pins, so an installer can use ``--require-hashes``. Two inputs that pin the same requirement (same name and marker) to
different versions are refused: a release has one set of dependencies. No hashes on a line is also refused.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQ = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9._+!-]+)(?:\s*;\s*(.+))?")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def parse(text: str) -> list[tuple[tuple[str, str], str, set[str]]]:
    """``[((name, marker), version, hashes)]`` of one export; comments and blank lines are skipped."""
    entries: list[tuple[tuple[str, str], str, set[str]]] = []
    logical = ""
    for raw in text.splitlines() + [""]:
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            logical += " " + line[:-1].strip()
            continue
        logical = (logical + " " + line).strip()
        if not logical:
            continue
        head = HASH.sub("", logical).strip()
        hashes = set(HASH.findall(logical))
        m = REQ.fullmatch(head)
        if m is None:
            raise ValueError(f"not a pinned requirement: {head[:80]!r}")
        if not hashes:
            raise ValueError(f"{m[1]}=={m[2]} has no --hash pin")
        entries.append(((m[1].lower().replace("_", "-"), (m[3] or "").strip()), m[2], hashes))
        logical = ""
    return entries


def merge(texts: list[str]) -> str:
    seen: dict[tuple[str, str], tuple[str, set[str]]] = {}
    for text in texts:
        for key, version, hashes in parse(text):
            if key in seen and seen[key][0] != version:
                raise ValueError(f"{key[0]}: pinned to {seen[key][0]} and {version} by different packages")
            seen.setdefault(key, (version, set()))[1].update(hashes)
    lines = []
    for (name, marker), (version, hashes) in sorted(seen.items()):
        req = f"{name}=={version}" + (f" ; {marker}" if marker else "")
        lines.append(" \\\n    ".join([req, *(f"--hash=sha256:{h}" for h in sorted(hashes))]))
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("exports", type=Path, nargs="+")
    a = ap.parse_args(argv)
    try:
        out = merge([p.read_text(encoding="utf-8") for p in a.exports])
    except (ValueError, OSError) as exc:
        print(f"constraints: {exc}", file=sys.stderr)
        return 1
    a.out.write_text(out, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
