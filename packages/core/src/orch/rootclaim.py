"""What a project folder already holds under the names orch's store takes there (finding B11-2).

A folder is an **orch workspace** only by orch's own marker: a ``config.json`` that is orch's (``"schema":
"orch.workspace/2"`` with a ``workspace`` id, or the v1 layout the importer knows), or orch's own
``.orch`` store. Generic names alone (``config.json``, ``events/``, ``tickets/``, ...) never make a folder "orch": many
projects have them. They are a **collision** instead: orch's store would write under the same name in the project
folder, and orch never writes over (or into) a project's own file. The one place that names what orch writes into the
root; ``orch setup`` and ``host.bootstrap`` both refuse through :func:`refusal`.

Read-only, ``O_NOFOLLOW`` below a directory fd (:mod:`orch.discovery`'s reader). Fail closed: a ``config.json`` that
cannot be read is not orch's, so it is a collision, never a pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from orch import discovery
from orch import layout as layout_mod

__all__ = ["WRITES", "RootView", "inspect", "refusal"]

#: the one name the store takes in the project root: ``.orch/`` (the project's own ``config.json``, ``events/``,
#: ``tickets/`` ... are no collision)
WRITES = (layout_mod.STORE_DIR,)


@dataclass(frozen=True)
class RootView:
    #: the folder holds an orch store (by orch's own marker)
    orch: bool
    #: project files/folders under a name orch's store would write
    collisions: tuple[str, ...]
    #: the folder holds an orch store from before the ``.orch/`` move (an orch config in the root): the plain message
    #: that says to delete the old files, ``None`` otherwise
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
        legacy = layout_mod.legacy_refusal(fd) if root_v2 else None
        if not orch and layout_mod.STORE_DIR in names:  # an orch store in .orch/ makes the folder a workspace too
            try:
                orch = layout_mod.resolve(fd, root) is not None
            except layout_mod.LayoutError:
                orch = False  # a linked or odd .orch is a collision (below), never a pass
    finally:
        os.close(fd)
    return RootView(
        orch=orch,
        collisions=tuple(sorted(n for n in WRITES if n in names)),
        legacy=legacy,
    )


def refusal(view: RootView) -> str | None:
    """The plain refusal for a **new** workspace in this folder, or ``None`` when it is free."""
    if view.legacy is not None:
        return view.legacy
    if view.orch:
        return "this folder is already an orch workspace (its config.json is orch's)"
    if layout_mod.STORE_DIR in view.collisions:
        return (
            "this project already has its own .orch/ (it is not an orch store); orch keeps its workspace in .orch/ "
            "and never writes into a project's folder. Rename or move it, or set up orch in another folder"
        )
    return None
