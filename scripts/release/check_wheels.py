#!/usr/bin/env python3
"""Check built wheels before they are signed.

    check_wheels.py --core X.whl --dashboard Y.whl --version V --channel C --commit SHA

The core wheel must carry orch/_build.json (written by stamp.py), valid JSON with version, channel, commit and
built_at; version, channel and commit must equal the release being built.
The dashboard wheel must carry the prebuilt page (orch_dashboard/static/index.html) and its add-on
manifest (orch_dashboard/orch-addon.json); see addons/dashboard/server/hatch_build.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

BUILD_KEYS = {"version", "channel", "commit", "built_at"}
DASHBOARD_FILES = ("orch_dashboard/static/index.html", "orch_dashboard/orch-addon.json")


def check_core(wheel: Path, expect: dict[str, str] | None = None) -> list[str]:
    try:
        with zipfile.ZipFile(wheel) as z:
            if "orch/_build.json" not in z.namelist():
                return [f"{wheel.name}: orch/_build.json is missing (run stamp.py before uv build)"]
            data = json.loads(z.read("orch/_build.json"))
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        return [f"{wheel.name}: cannot read the wheel or orch/_build.json: {exc}"]
    if not isinstance(data, dict) or not BUILD_KEYS <= set(data):
        return [f"{wheel.name}: orch/_build.json lacks one of {sorted(BUILD_KEYS)}"]
    return [
        f"{wheel.name}: _build.json {k} is {data[k]!r}, the release being built has {v!r}"
        for k, v in (expect or {}).items()
        if data[k] != v
    ]


def check_dashboard(wheel: Path) -> list[str]:
    try:
        with zipfile.ZipFile(wheel) as z:
            names = set(z.namelist())
            sizes = {i.filename: i.file_size for i in z.infolist()}
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"{wheel.name}: cannot read the wheel: {exc}"]
    problems = [f"{wheel.name}: {n} is missing" for n in DASHBOARD_FILES if n not in names]
    if "orch_dashboard/static/index.html" in names and sizes["orch_dashboard/static/index.html"] == 0:
        problems.append(f"{wheel.name}: orch_dashboard/static/index.html is empty")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--core", type=Path, required=True)
    ap.add_argument("--dashboard", type=Path, required=True)
    for k in ("version", "channel", "commit"):
        ap.add_argument(f"--{k}", required=True, help=f"the release's {k}; must equal the stamped _build.json")
    a = ap.parse_args(argv)
    problems = check_core(a.core, {k: getattr(a, k) for k in ("version", "channel", "commit")}) + check_dashboard(
        a.dashboard
    )
    for p in problems:
        print(f"check_wheels: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
