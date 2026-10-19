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
from orch import layout as layout_mod

__all__ = ["MARKERS", "WRITES", "RootView", "inspect", "markers", "refusal", "writes"]

#: legacy layout: names the store and setup create directly in the project root (``addons/`` holds the add-on
#: packages). New layout (``orch.layout.WRITE_NEW``): the store lives in ``.orch/``, which is the only name taken.
WRITES = ("config.json", "keys.jsonl", "events", "tickets", ".state", "addons")
#: names only orch makes: an init in progress (or a crashed one), and in the legacy layout the reserved store folder
MARKERS = (".orch-init.lock", ".orch")
_MIGRATING = ".orch-migrating"


def writes() -> tuple[str, ...]:
    """The names a NEW workspace's store takes in the project root: ``.orch`` alone in the new layout (the project's
    own ``config.json``, ``events/``, ``tickets/`` ... are no collision there), the legacy list otherwise."""
    return (layout_mod.STORE_DIR,) if layout_mod.WRITE_NEW else WRITES


def markers() -> tuple[str, ...]:
    """Names only orch makes. New layout: an init in progress and a store move in progress (``.orch`` itself is either
    orch's store, which makes the folder a workspace, or the project's own, which is a collision)."""
    return (".orch-init.lock", _MIGRATING) if layout_mod.WRITE_NEW else MARKERS


@dataclass(frozen=True)
class RootView:
    #: the folder holds an orch store (by orch's own marker)
    orch: bool
    #: orch's own marker names present (``.orch-init.lock``, ``.orch``) without an orch config
    markers: tuple[str, ...]
    #: project files/folders under a name orch's store would write
    collisions: tuple[str, ...]
    #: the folder holds an orch store from before the ``.orch/`` move (an orch config in the root): the plain message
    #: that says to delete the old files, ``None`` otherwise (or while the suite allows legacy fixtures)
    legacy: str | None = None


def inspect(root: str | os.PathLike[str]) -> RootView:
    """Classify ``root``. Raises ``OSError`` when the folder cannot be opened or listed."""
    fd = os.open(root, discovery._DIR | discovery._NOFOLLOW)
    try:
        names = set(os.listdir(fd))
        got = discovery._config_at(fd) if "config.json" in names else None
        doc = got[0] if got is not None else None
        root_v2 = discovery.v2_config(doc) is not None
        orch = root_v2 or discovery._is_v1(fd, doc)
        legacy = layout_mod.legacy_refusal(fd) if root_v2 and not layout_mod.LEGACY_ALLOWED else None
        if not orch and layout_mod.STORE_DIR in names:  # an orch store in .orch/ makes the folder a workspace too
            try:
                orch = layout_mod.resolve(fd, root) is not None
            except layout_mod.LayoutError:
                orch = False  # a linked or odd .orch is a collision (below), never a pass
    finally:
        os.close(fd)
    return RootView(
        orch=orch,
        markers=tuple(n for n in markers() if n in names),
        collisions=tuple(sorted(n for n in writes() if n in names)),
        legacy=legacy,
    )


def _shown(name: str) -> str:
    return name if name in ("config.json", "keys.jsonl") else f"{name}/"


def _own_dot_orch(view: RootView) -> bool:
    return layout_mod.WRITE_NEW and layout_mod.STORE_DIR in view.collisions


def refusal(view: RootView, *, init_lock_ok: bool = False) -> str | None:
    """The plain refusal for a **new** workspace in this folder, or ``None`` when it is free. ``init_lock_ok``: the
    caller is ``orch init`` itself (its ``flock`` on ``.orch-init.lock`` decides; a crashed init's lock is reused)."""
    if view.legacy is not None:
        return view.legacy
    if view.orch:
        return "this folder is already an orch workspace (its config.json is orch's)"
    held = [m for m in view.markers if not (init_lock_ok and m == ".orch-init.lock")]
    if _MIGRATING in held:
        return (
            "an unfinished store move is in this folder (.orch-migrating); "
            "delete the old store files and .orch-migrating, then run orch setup again"
        )
    if held:
        return (
            f"this folder holds orch's own {', '.join(held)} from an earlier or running orch init; "
            "remove it if no init is running, then retry"
        )
    if _own_dot_orch(view):
        return (
            "this project already has its own .orch/ (it is not an orch store); orch keeps its workspace in .orch/ "
            "and never writes into a project's folder. Rename or move it, or set up orch in another folder"
        )
    if view.collisions:
        names = ", ".join(_shown(n) for n in view.collisions)
        return (
            f"this project already has its own {names}; orch keeps its workspace files under the same "
            f"name{'s' if len(view.collisions) > 1 else ''} in the project folder and never overwrites a project's "
            "file. Rename or move it, or set up orch in another folder"
        )
    return None
