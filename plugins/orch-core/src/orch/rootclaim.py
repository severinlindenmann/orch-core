"""What a project folder already holds under the names orch's store takes there (finding B11-2).

A folder is an **orch workspace** only by orch's own marker: a ``config.json`` that is orch's (``"schema":
"orch.workspace/2"`` with a ``workspace`` id, or the v1 layout the importer knows), or orch's own ``.orch-init.lock`` /
``.orch``. Generic names alone (``config.json``, ``events/``, ``tickets/``, ...) never make a folder "orch": many
projects have them. They are a **collision** instead: orch's store would write under the same name in the project
folder, and orch never writes over (or into) a project's own file. The one place that names what orch writes into the
root; ``orch setup``, ``orch init`` and ``host.bootstrap`` all refuse through :func:`refusal`.

Read-only, ``O_NOFOLLOW`` below a directory fd (:mod:`orch.discovery`'s reader). Fail closed: a ``config.json`` that
cannot be read is not orch's, so it is a collision, never a pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from orch import discovery

__all__ = ["MARKERS", "WRITES", "RootView", "inspect", "refusal"]

#: names the store and setup create directly in the project root (``addons/`` holds the add-on packages)
WRITES = ("config.json", "keys.jsonl", "events", "tickets", ".state", "addons")
#: names only orch makes: an init in progress (or a crashed one), the reserved store folder
MARKERS = (".orch-init.lock", ".orch")


@dataclass(frozen=True)
class RootView:
    #: the folder holds an orch store (by orch's own marker)
    orch: bool
    #: orch's own marker names present (``.orch-init.lock``, ``.orch``) without an orch config
    markers: tuple[str, ...]
    #: project files/folders under a name orch's store would write
    collisions: tuple[str, ...]


def inspect(root: str | os.PathLike[str]) -> RootView:
    """Classify ``root``. Raises ``OSError`` when the folder cannot be opened or listed."""
    fd = os.open(root, discovery._DIR | discovery._NOFOLLOW)
    try:
        names = set(os.listdir(fd))
        got = discovery._config_at(fd) if "config.json" in names else None
        doc = got[0] if got is not None else None
        orch = discovery.v2_config(doc) is not None or discovery._is_v1(fd, doc)
    finally:
        os.close(fd)
    return RootView(
        orch=orch,
        markers=tuple(n for n in MARKERS if n in names),
        collisions=tuple(sorted(n for n in WRITES if n in names)),
    )


def _shown(name: str) -> str:
    return name if name in ("config.json", "keys.jsonl") else f"{name}/"


def refusal(view: RootView, *, init_lock_ok: bool = False) -> str | None:
    """The plain refusal for a **new** workspace in this folder, or ``None`` when it is free. ``init_lock_ok``: the
    caller is ``orch init`` itself (its ``flock`` on ``.orch-init.lock`` decides; a crashed init's lock is reused)."""
    if view.orch:
        return "this folder is already an orch workspace (its config.json is orch's)"
    markers = [m for m in view.markers if not (init_lock_ok and m == ".orch-init.lock")]
    if markers:
        return (
            f"this folder holds orch's own {', '.join(markers)} from an earlier or running orch init; "
            "remove it if no init is running, then retry"
        )
    if view.collisions:
        names = ", ".join(_shown(n) for n in view.collisions)
        return (
            f"this project already has its own {names}; orch keeps its workspace files under the same "
            f"name{'s' if len(view.collisions) > 1 else ''} in the project folder and never overwrites a project's "
            "file. Rename or move it, or set up orch in another folder"
        )
    return None
