"""The check ``orch setup`` makes before it starts: is there an older (or foreign) orch install on this computer?

An identity (``person/``, ``person-key``) with no host registry next to it is what an install from an older version, or
a clean-up that stopped half-way, leaves behind: its device and identity then confuse a new setup (signing refuses
with ``sign.convenience_unavailable``). There is no migration: the answer is to remove it with ``orch host prune``."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

__all__ = ["MESSAGE", "older_install"]

MESSAGE = "Found an older orch install. Run `orch host prune` first."


def older_install(env: Mapping[str, str]) -> str | None:
    """:data:`MESSAGE` (with what was found) when the state folder holds an identity that no registry explains."""
    from orch.ops.runtime import state_dir

    try:
        state = state_dir(env)
    except Exception:  # noqa: BLE001 - no state folder to look at: nothing found
        return None
    identity = [n for n in ("person", "person-key") if os.path.lexists(state / n)]
    if not identity:
        return None
    registry = state / "host" / "workspaces.json"
    try:
        doc = json.loads(registry.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"{MESSAGE} ({_names(state, identity)} without a host registry)"
    except (OSError, ValueError):
        return f"{MESSAGE} (the host registry in {state} cannot be read)"
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("workspaces"), list):
        return f"{MESSAGE} (the host registry in {state} has an unknown shape)"
    return None


def _names(state: Path, names: list[str]) -> str:
    return ", ".join(os.fspath(state / n) for n in names)
