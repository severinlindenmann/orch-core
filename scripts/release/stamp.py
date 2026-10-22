#!/usr/bin/env python3
"""Stamp a build: ``stamp.py VERSION CHANNEL COMMIT``.

Rewrites the version in plugins/orch-core/pyproject.toml and src/orch/__init__.py and writes src/orch/_build.json.
(The dashboard pin is added by the dashboard task.) Run before ``uv build``; never commit the result.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "plugins" / "orch-core"
VERSION = re.compile(r"2\.\d+\.\d+(\.dev\d+)?")
CHANNEL = re.compile(r"(main|develop|test-[a-z0-9][a-z0-9-]{0,30})")
COMMIT = re.compile(r"[0-9a-f]{7,40}")


def stamp(version: str, channel: str, commit: str, *, core: Path = CORE, now: datetime.datetime | None = None) -> None:
    if not VERSION.fullmatch(version):
        raise ValueError(f"bad version: {version!r}")
    if not CHANNEL.fullmatch(channel):
        raise ValueError(f"bad channel: {channel!r}")
    if not COMMIT.fullmatch(commit):
        raise ValueError(f"bad commit: {commit!r}")
    pyproject = core / "pyproject.toml"
    text, n = re.subn(r'(?m)^version = "[^"]*"$', f'version = "{version}"', pyproject.read_text(), count=1)
    if n != 1:
        raise ValueError("no version line in pyproject.toml")
    pyproject.write_text(text)
    init = core / "src" / "orch" / "__init__.py"
    text, n = re.subn(r'(?m)^__version__ = "[^"]*"$', f'__version__ = "{version}"', init.read_text(), count=1)
    if n != 1:
        raise ValueError("no __version__ line in orch/__init__.py")
    init.write_text(text)
    built_at = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp_json = {"version": version, "channel": channel, "commit": commit, "built_at": built_at}
    (core / "src" / "orch" / "_build.json").write_text(json.dumps(stamp_json, indent=2) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: stamp.py VERSION CHANNEL COMMIT", file=sys.stderr)
        return 2
    try:
        stamp(*argv)
    except ValueError as e:
        print(f"stamp: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
