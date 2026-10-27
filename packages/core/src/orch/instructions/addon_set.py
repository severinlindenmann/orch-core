"""The add-on set a workspace's instructions are written for (design addon-skills D2, D3).

``collect_rows`` reads the replayed ``Addon`` records of the log and the packages on disk (the same way ``orch addon
list`` does, through :func:`orch.addons.registry.state_of`) and returns one :class:`~.agents_md.AddonRow` per granted,
not purged add-on. A built-in add-on whose package folder is not on disk loads its manifest from
``orch/addons/manifests`` (the package a built-in grant names).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .agents_md import AddonRow

__all__ = ["addons_line", "collect_rows", "known_worktrees"]


def collect_rows(store_root: str | os.PathLike[str], view: Mapping[str, Any]) -> list[AddonRow]:
    """``view`` is ``WorkspaceView.addons`` (or replayed ``Addon`` records); ``store_root`` the store folder that holds
    ``addons/``. Never raises for a damaged package: that add-on is ``missing`` (or ``changed``)."""
    from orch.addons import builtin, install
    from orch.addons.manifest import ManifestError, load_manifest
    from orch.addons.package import MANIFEST_FILE, PackageError, package_digest
    from orch.addons.registry import state_of

    rows: list[AddonRow] = []
    for name, grant in sorted(install.as_addons(view).items()):
        if grant.purged:
            continue
        loaded = None
        pkg = None
        try:
            pkg, manifest = install.read_installed(store_root, name)
            loaded = (manifest, pkg.digest)
        except (PackageError, ManifestError, OSError):
            if name in builtin.NAMES and not (Path(store_root) / install.ADDONS_DIR / name).exists():
                try:
                    raw = builtin.manifest_bytes(name)
                    loaded = (load_manifest(raw), package_digest({MANIFEST_FILE: raw}))
                except (ManifestError, PackageError, ValueError, OSError):
                    loaded = None
        state = state_of(grant, loaded)
        agents_md, skill, speaks = "", None, True
        if loaded is not None:
            manifest = loaded[0]
            agents_md = manifest.agents_md
            speaks = bool(agents_md or manifest.skill)
            if state == "active" and pkg is not None and manifest.skill:
                skill = pkg.files.get(manifest.skill + "/SKILL.md")
        rows.append(AddonRow(name, state, grant.version, grant.package_sha256, agents_md, skill, speaks))
    return rows


def addons_line(rows: Sequence[AddonRow]) -> str | None:
    """The line of the session-start and pre-compact hooks (design D4), or ``None`` when no add-on has anything to say
    and none is off: ``add-ons: a, b · off: c · read a skill before using one``."""
    on = sorted(r.name for r in rows if r.active and r.speaks)
    off = sorted(r.name for r in rows if not r.active and r.speaks)
    if not on and not off:
        return None
    return f"add-ons: {', '.join(on) or 'none'} · off: {', '.join(off) or 'none'} · read a skill before using one"


def known_worktrees(caps: Any) -> list[str]:
    """The registered worktree paths of the workspace as the host's git reader knows them (``worktree add``): the
    places besides the workspace root where an agent works. ``[]`` when git is not available (direct mode, a host
    without a reader), or on any problem reading the registrations: the sync then covers the root only."""
    try:
        git = caps.git
        if not git.enabled or git.reader is None:
            return []
        return sorted({slot["reg"]["path"] for _uid, _repo, slot in git.reader.pins.worktrees()})
    except Exception:  # noqa: BLE001 - a hint of where to also write; never a reason to fail a sync
        return []
