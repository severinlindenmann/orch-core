"""Entry point for the `orch` command (skeleton)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--version"]:
        print("orch v2 (in development)")
        return 0
    print("orch: not implemented yet", file=sys.stderr)
    return 2
