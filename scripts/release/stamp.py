#!/usr/bin/env python3
"""Stamp a build: ``stamp.py VERSION CHANNEL COMMIT``.

Rewrites the version in packages/core/pyproject.toml and src/orch/__init__.py, writes src/orch/_build.json, and
sets packages/addons/dashboard/server/pyproject.toml to the same version with the exact pin ``orch-core==VERSION``. Every
rewrite must match exactly one line, otherwise nothing is written. Run before ``uv build``; never commit the result.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "packages" / "core"
DASHBOARD = ROOT / "packages" / "addons" / "dashboard" / "server"
VERSION = re.compile(r"2\.\d+\.\d+(\.dev\d+)?")
CHANNEL = re.compile(r"(main|develop|test-[a-z0-9][a-z0-9-]{0,30})")
COMMIT = re.compile(r"[0-9a-f]{7,40}")


def _sub_one(pattern: str, repl: str, text: str, what: str) -> str:
    """Replace the one line matching ``pattern``; zero or several matches are an error."""
    found = len(re.findall(pattern, text))
    if found != 1:
        raise ValueError(f"{what}: expected exactly one match, found {found}")
    return re.sub(pattern, lambda _m: repl, text)


def stamp(
    version: str,
    channel: str,
    commit: str,
    *,
    core: Path = CORE,
    dashboard: Path,
    now: datetime.datetime | None = None,
) -> None:
    if not VERSION.fullmatch(version):
        raise ValueError(f"bad version: {version!r}")
    if not CHANNEL.fullmatch(channel):
        raise ValueError(f"bad channel: {channel!r}")
    if not COMMIT.fullmatch(commit):
        raise ValueError(f"bad commit: {commit!r}")
    ver_line = r'(?m)^version = "[^"]*"$'
    new_ver = f'version = "{version}"'
    pyproject, init, dash = core / "pyproject.toml", core / "src" / "orch" / "__init__.py", dashboard / "pyproject.toml"
    # compute every rewrite first: a refusal leaves all files untouched
    pyproject_text = _sub_one(ver_line, new_ver, pyproject.read_text(), "orch-core pyproject.toml version")
    init_text = _sub_one(
        r'(?m)^__version__ = "[^"]*"$', f'__version__ = "{version}"', init.read_text(), "orch/__init__.py __version__"
    )
    dash_text = dash.read_text()
    dash_text = _sub_one(ver_line, new_ver, dash_text, "dashboard pyproject.toml version")
    dash_text = _sub_one(r'"orch-core>=[^"]*"', f'"orch-core=={version}"', dash_text, "dashboard orch-core dependency")
    built_at = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp_json = {"version": version, "channel": channel, "commit": commit, "built_at": built_at}
    pyproject.write_text(pyproject_text)
    init.write_text(init_text)
    dash.write_text(dash_text)
    (core / "src" / "orch" / "_build.json").write_text(json.dumps(stamp_json, indent=2) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: stamp.py VERSION CHANNEL COMMIT", file=sys.stderr)
        return 2
    try:
        stamp(*argv, dashboard=DASHBOARD)
    except ValueError as e:
        print(f"stamp: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
