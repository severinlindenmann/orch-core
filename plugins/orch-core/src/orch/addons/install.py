"""Installed addons: where a package lives, how a grant is built from it, how the registry is loaded (§8.1).

An addon is installed as the directory ``<workspace>/addons/<name>/``. Nothing here trusts the directory: a package is
read once (:func:`orch.addons.package.read_package`), and everything follows from those bytes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch.addons.manifest import Manifest, ManifestError, derive_binds, load_package_manifest
from orch.addons.package import Package, PackageError, read_package
from orch.addons.registry import Registry
from orch.model.types import Addon

__all__ = [
    "ADDONS_DIR",
    "as_addons",
    "grant_event",
    "installed_names",
    "load_registry",
    "package_dir",
    "read_installed",
]

ADDONS_DIR = "addons"
_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}(?![\s\S])")


def package_dir(root: str | os.PathLike[str], name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise PackageError("an addon name is [a-z][a-z0-9-]{0,39}")
    return Path(root) / ADDONS_DIR / name


def installed_names(root: str | os.PathLike[str]) -> list[str]:
    """Names of the directories under ``<workspace>/addons/`` (only well-formed names; nothing is read)."""
    base = Path(root) / ADDONS_DIR
    try:
        if base.is_symlink() or not base.is_dir():
            return []
        return sorted(n for n in os.listdir(base) if _NAME.fullmatch(n))
    except OSError:
        return []


def read_installed(root: str | os.PathLike[str], name: str) -> tuple[Package, Manifest]:
    """The package and manifest of an installed addon. Raises :class:`PackageError` or :class:`ManifestError`."""
    base = Path(root) / ADDONS_DIR
    if base.is_symlink():
        raise PackageError("addons/ is a symbolic link")
    pkg = read_package(package_dir(root, name))
    return pkg, load_package_manifest(pkg, name)


def grant_event(manifest: Manifest, package: Package) -> dict[str, Any]:
    """The payload of ``addon.granted`` for this package: exactly the manifest's capabilities, ``binds`` derived."""
    return {
        "type": "addon.granted",
        "name": manifest.name,
        "version": manifest.version,
        "package_sha256": package.digest,
        "capabilities": manifest.capabilities,
        "binds": derive_binds(manifest),
    }


def as_addons(view: Mapping[str, Any]) -> dict[str, Addon]:
    """``Addon`` records from ``WorkspaceView.addons`` (or from the model's own, which pass through)."""
    out: dict[str, Addon] = {}
    for name, a in view.items():
        if isinstance(a, Addon):
            out[name] = a
        else:
            out[name] = Addon(
                name,
                a["version"],
                a["package_sha256"],
                list(a["capabilities"]),
                dict(a["binds"]),
                enabled=bool(a["enabled"]),
                purged=bool(a["purged"]),
            )
    return out


def load_registry(root: str | os.PathLike[str], view: Mapping[str, Any]) -> Registry:
    """The registry of a workspace: the replayed addons (``WorkspaceView.addons``) and the manifests found on disk. A
    package that cannot be read is simply not loaded (the addon is then ``missing``)."""
    addons = as_addons(view)
    loaded: dict[str, tuple[Manifest, str]] = {}
    for name, a in addons.items():
        if a.purged or not a.enabled:
            continue
        try:
            pkg, manifest = read_installed(root, name)
        except (PackageError, ManifestError):
            continue
        loaded[name] = (manifest, pkg.digest)
    return Registry(addons, loaded)
