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


#: written by ``orch setup`` of this version when it starts (nothing older was found): a setup that crashed after it
#: wrote the identity but before the workspace was registered is resumed, never mistaken for an older install
MARKER = "setup-started"


def _mark(state: Path) -> None:
    from orch import __version__

    try:
        os.makedirs(state, mode=0o700, exist_ok=True)
        fd = os.open(state / MARKER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(__version__ + "\n")
    except OSError:
        pass  # the check then has no marker: at worst a later rerun is asked to prune, never data lost


def _marked(state: Path) -> bool:
    from orch import __version__

    try:
        return (state / MARKER).read_text(encoding="utf-8").strip() == __version__
    except (OSError, ValueError):
        return False


def older_install(env: Mapping[str, str], *, mark: bool = False) -> str | None:
    """:data:`MESSAGE` (with what was found) when the state folder holds an identity that no registry explains and no
    setup of this version started it. ``mark=True`` (``orch setup``): when nothing older is found, record that this
    version's setup started here."""
    from orch.ops.runtime import state_dir

    try:
        state = state_dir(env)
    except Exception:  # noqa: BLE001 - no state folder to look at: nothing found
        return None
    found = _older(state)
    if found is None and mark:
        _mark(state)
    return found


def _older(state: Path) -> str | None:
    identity = [n for n in ("person", "person-key") if os.path.lexists(state / n)]
    if not identity or _marked(state):
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
