"""Build stamp: the version, channel and commit written into the wheel by scripts/release/stamp.py.

A source checkout has no ``_build.json`` and reports the development version; an unreadable or malformed stamp
is treated the same way (the version line must never fail).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = ["build_info", "version_line"]

_STAMP = Path(__file__).with_name("_build.json")
_DEV_VERSION = "2.0.0.dev0"
_COMMIT = re.compile(r"[0-9a-f]{7,64}")


def _vcs_commit() -> str | None:
    """W29 (#1167): the commit a git install was made from (``uv tool install git+...@<rev>``): PEP 610
    ``direct_url.json`` of the installed distribution, ``vcs_info.commit_id``. ``None`` for anything else (an editable
    checkout, a wheel, an unreadable or odd file); never raises."""
    try:
        from importlib import metadata

        raw = metadata.distribution("orch-core").read_text("direct_url.json")
        commit = (json.loads(raw or "null") or {}).get("vcs_info", {}).get("commit_id")
    except Exception:  # noqa: BLE001 - the version line must never fail
        return None
    return commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None


def build_info() -> dict[str, Any]:
    """``{version, channel, commit, built_at, development}``; a development build has no channel, and its commit is the
    git revision it was installed from when the installer recorded one (W29), else None."""
    try:
        raw = json.loads(_STAMP.read_text(encoding="utf-8"))
        info = {k: raw[k] for k in ("version", "channel", "commit", "built_at")}
        if not all(isinstance(v, str) and v and v.isprintable() for v in info.values()):
            raise ValueError("bad stamp")
    except (OSError, ValueError, KeyError, TypeError):
        dev = {"version": _DEV_VERSION, "channel": None, "commit": None, "built_at": None, "development": True}
        return {**dev, "commit": _vcs_commit()}
    return {**info, "development": False}


def version_line() -> str:
    info = build_info()
    if info["development"]:
        git = f", git {info['commit'][:7]}" if info["commit"] else ""
        return f"orch {info['version']} (development build{git})"
    return f"orch {info['version']} (channel {info['channel']}, commit {str(info['commit'])[:7]})"
