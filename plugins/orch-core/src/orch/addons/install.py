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

from orch.addons.manifest import Manifest, ManifestError, declarations, load_package_manifest
from orch.addons.package import Package, PackageError, read_package
from orch.addons.registry import Registry
from orch.model.types import Addon

__all__ = [
    "ADDONS_DIR",
    "as_addons",
    "grant_event",
    "installed_names",
    "review_lines",
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
    package_dir(root, name)  # validates the name
    try:  # ``addons`` is opened without following a link, and the package is read relative to it: no race in between
        fd = os.open(
            Path(root) / ADDONS_DIR,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise PackageError("addons/ cannot be opened (missing, or a symbolic link)") from None
    try:
        pkg = read_package(name, dir_fd=fd)
    finally:
        os.close(fd)
    return pkg, load_package_manifest(pkg, name)


def grant_event(manifest: Manifest, package: Package) -> dict[str, Any]:
    """The payload of ``addon.granted`` for this package: exactly the manifest's capabilities, its declarations."""
    return {
        "type": "addon.granted",
        "name": manifest.name,
        "version": manifest.version,
        "package_sha256": package.digest,
        "capabilities": manifest.capabilities,
        **declarations(manifest),
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
                {k: dict(v) for k, v in a["fields"].items()},
                [dict(x) for x in a["sections"]],
                list(a["artifact_kinds"]),
                enabled=bool(a["enabled"]),
                purged=bool(a["purged"]),
            )
    return out


def review_lines(manifest: Manifest, package: Package, event: Mapping[str, Any], previous: Addon | None) -> list[str]:
    """What the owner reads before signing a grant (§8.1): what runs, what it may do, what it tells agents, who may
    write its fields, and on a re-grant what changed since the last one. Text from the package is shown as data,
    escaped to printable ASCII."""

    def show(x: object) -> str:
        return ascii(str(x))[1:-1]  # printable ASCII only: control characters and look-alikes are escaped

    out = [
        f"addon {manifest.name} {manifest.version} ({show(manifest.title)})",
        f"package {package.digest}",
        f"command {show(' '.join(manifest.entry_cmd))}",
        f"capabilities {', '.join(event['capabilities']) or 'none'} (granted exactly as the package declares them)",
    ]
    if manifest.agents_md:
        out.append(f"agents see: - addon {manifest.name} (hint, grants nothing): {show(manifest.agents_md)}")
    for rule in manifest.needs:
        out.append(f"needs rule {rule['id']} for {', '.join(rule['who'])}: {show(rule['text'])}")
    for fname, spec in event["fields"].items():
        who = ", ".join(spec["set_by"])
        gate = f", bound to {', '.join(spec['gate'])}" if spec.get("gate") else ""
        out.append(f"field {fname} ({spec['type']}) set by {who}{gate}")
    for sec in event["sections"]:
        out.append(f"section {sec['id']} for {', '.join(sec['types'])}")
    if event["artifact_kinds"]:
        out.append(f"artifact kinds {', '.join(event['artifact_kinds'])}")
    if previous is not None:
        out.append(f"previous grant: {previous.version}, package {previous.package_sha256[:19]}")
        out += [f"changed: {x}" for x in _changes(previous, event)] or ["changed: the package bytes only"]
    return out


def _changes(old: Addon, new: Mapping[str, Any]) -> list[str]:
    out = []
    if old.version != new["version"]:
        out.append(f"version {old.version} -> {new['version']}")
    if sorted(old.capabilities) != list(new["capabilities"]):
        out.append(f"capabilities {sorted(old.capabilities)} -> {list(new['capabilities'])}")
    for f in sorted(set(old.fields) | set(new["fields"])):
        a, b = old.fields.get(f), new["fields"].get(f)
        if a is None:
            out.append(f"field {f} added")
        elif b is None:
            out.append(f"field {f} removed")
        elif a != b:
            out.append(f"field {f} changed")
    if [s["id"] for s in old.sections] != [s["id"] for s in new["sections"]] or old.sections != new["sections"]:
        out.append("sections changed")
    if sorted(old.artifact_kinds) != list(new["artifact_kinds"]):
        out.append("artifact kinds changed")
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
