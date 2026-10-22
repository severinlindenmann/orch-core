"""Build stamp: the version, channel and commit written into the wheel by scripts/release/stamp.py.

A source checkout has no ``_build.json`` and reports the development version; an unreadable or malformed stamp
is treated the same way (the version line must never fail).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["build_info", "version_line"]

_STAMP = Path(__file__).with_name("_build.json")
_DEV_VERSION = "2.0.0.dev0"


def build_info() -> dict[str, Any]:
    """``{version, channel, commit, built_at, development}``; channel and commit are None for a development build."""
    try:
        raw = json.loads(_STAMP.read_text(encoding="utf-8"))
        info = {k: raw[k] for k in ("version", "channel", "commit", "built_at")}
        if not all(isinstance(v, str) and v and v.isprintable() for v in info.values()):
            raise ValueError("bad stamp")
    except (OSError, ValueError, KeyError, TypeError):
        return {"version": _DEV_VERSION, "channel": None, "commit": None, "built_at": None, "development": True}
    return {**info, "development": False}


def version_line() -> str:
    info = build_info()
    if info["development"]:
        return f"orch {info['version']} (development build)"
    return f"orch {info['version']} (channel {info['channel']}, commit {str(info['commit'])[:7]})"
