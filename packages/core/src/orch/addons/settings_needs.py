"""Add-ons that need a workspace setting before they can be on (AE4 of the ``addon_enable`` design, 19 Oct 2026).

Publish needs the address of its server (``settings.publish``). The ONE rule: an add-on of
:data:`NEEDS_SETTINGS` is never on while its setting is unset.
Every path reads this module, so the CLI (``orch addon grant``), the dashboard and the signer's sequence all hold:

* ``addon.grant`` of such an add-on is refused while its setting is unset (:func:`grant_refusal`);
* ``settings.set`` that clears the setting (``null``) is refused while the add-on is on (:func:`clear_refusal`).

Replay is not touched: a workspace that is already "on, not set up" keeps working and is offered "Set up".
(The name ``needs.py`` is taken by the "needs you" entries of the rules language.)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "NEEDS_SETTINGS",
    "TITLES",
    "clear_refusal",
    "grant_refusal",
    "is_on",
    "is_set",
    "preview_grant",
]

#: add-on name -> the ``settings.set`` key it needs
NEEDS_SETTINGS: dict[str, str] = {"publish": "publish"}
#: the words a person reads (the same titles as the setup catalog; a test pins the equality)
TITLES: dict[str, str] = {"publish": "Publish"}


def _empty(value: Any) -> bool:
    """Nothing set: ``None``."""
    return value is None


def is_set(settings: Mapping[str, Any], name: str) -> bool:
    """Is the setting that ``name`` needs set? An add-on that needs none counts as set."""
    key = NEEDS_SETTINGS.get(name)
    return key is None or not _empty(settings.get(key))


def is_on(addons: Mapping[str, Any], name: str) -> bool:
    """Is ``name`` granted, enabled and not purged? ``addons`` is ``WorkspaceView.addons`` (or the model's own)."""
    from orch.addons.install import as_addons

    found = as_addons(addons).get(name)
    return found is not None and found.enabled and not found.purged


def grant_refusal(settings: Mapping[str, Any], name: str) -> str | None:
    """The reason ``addon.grant`` of ``name`` is refused now, or ``None``: its setting is unset (AE4)."""
    if name in NEEDS_SETTINGS and not is_set(settings, name):
        return f"{TITLES[name]} needs its settings first: set them up, then turn it on"
    return None


def clear_refusal(changes: Mapping[str, Any], addons: Mapping[str, Any]) -> str | None:
    """The reason a ``settings.set`` of ``changes`` is refused, or ``None``: it clears the setting (``null``) of an
    add-on that is on (AE4). Clearing while the add-on is off is allowed."""
    for name, key in NEEDS_SETTINGS.items():
        if key in changes and _empty(changes[key]) and is_on(addons, name):
            return f"{TITLES[name]} is on and needs this setting: turn it off first"
    return None


def preview_grant(root: str | os.PathLike[str], name: str) -> dict[str, Any]:
    """The body of the ``addon.granted`` event that ``addon.grant`` of ``name`` would sign now (no ``type``): from the
    installed package, or for a built-in add-on that is not installed from the shipped bytes in memory (the same bytes
    ``addon.grant`` writes). Never writes. Raises ``KeyError`` (no such add-on), ``PackageError``, ``ManifestError``."""
    from orch.addons import builtin, install, manifest, package

    directory = install.package_dir(root, name)  # validates the name
    if Path(directory).exists():
        pkg, mf = install.read_installed(root, name)
    elif name in builtin.NAMES:
        files = {package.MANIFEST_FILE: builtin.manifest_bytes(name)}
        pkg = package.Package(files, package.package_digest(files))
        mf = manifest.load_package_manifest(pkg, name)
    else:
        raise KeyError(name)
    body = install.grant_event(mf, pkg)
    body.pop("type")
    return body
