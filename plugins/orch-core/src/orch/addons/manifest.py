"""The addon manifest: load, validate, derive ``binds`` (ticket-format §8, §8.1).

``load_manifest`` takes the bytes of ``orch-addon.json`` (from :func:`orch.addons.package.read_package`), parses them
with the strict JSON parser and validates them against the ``addon-manifest`` schema. ``derive_binds`` is the only
place that turns a manifest into the ``binds`` of an ``addon.granted`` event; replay never looks at a manifest again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from orch import canon, schema
from orch.addons.package import MANIFEST_FILE, Package

__all__ = ["CAPABILITIES", "Manifest", "ManifestError", "derive_binds", "load_manifest", "load_package_manifest"]

CAPABILITIES = ("serve_http", "spawn_agent", "pty", "network", "git_push")  # the closed list (§11.4)
_PERSON = re.compile(r"p_[a-z0-9_]{1,64}(?![\s\S])")


class ManifestError(ValueError):
    """The manifest is not valid; the message says where and why, never what a hostile file contained."""


@dataclass(frozen=True)
class Manifest:
    doc: dict[str, Any]

    @property
    def name(self) -> str:
        return self.doc["name"]

    @property
    def version(self) -> str:
        return self.doc["version"]

    @property
    def title(self) -> str:
        return self.doc["title"]

    @property
    def entry_cmd(self) -> list[str]:
        return list(self.doc["entry"]["cmd"])

    @property
    def fields(self) -> dict[str, dict[str, Any]]:
        return self.doc.get("fields", {})

    @property
    def sections(self) -> list[dict[str, Any]]:
        return self.doc.get("sections", [])

    @property
    def artifact_kinds(self) -> list[dict[str, Any]]:
        return self.doc.get("artifact_kinds", [])

    @property
    def capabilities(self) -> list[str]:
        return sorted(self.doc.get("capabilities", []))

    @property
    def needs(self) -> list[dict[str, Any]]:
        return self.doc.get("needs", [])

    @property
    def agents_md(self) -> str:
        return self.doc.get("agents_md", "")


def load_manifest(raw: bytes) -> Manifest:
    """Parse and validate the bytes of an ``orch-addon.json``. Raises :class:`ManifestError`."""
    try:
        doc = canon.loads_strict(raw)
    except (canon.JcsError, ValueError, RecursionError, UnicodeError):
        raise ManifestError("orch-addon.json is not strict JSON") from None
    try:
        schema.validate("addon-manifest", doc)
    except schema.SchemaError as e:
        raise ManifestError(
            f"orch-addon.json{e.path}: {e.message}".encode("ascii", "backslashreplace").decode()
        ) from None
    return Manifest(doc)


def load_package_manifest(package: Package, directory_name: str | None = None) -> Manifest:
    """The manifest of a read package; ``directory_name`` (when given) must equal the manifest's ``name``."""
    m = load_manifest(package.files[MANIFEST_FILE])
    if directory_name is not None and m.name != directory_name:
        raise ManifestError(f"the directory is {directory_name!r} but {MANIFEST_FILE} names the addon {m.name!r}")
    return m


def derive_binds(m: Manifest) -> dict[str, Any]:
    """``binds`` of the ``addon.granted`` event (§5.4.2):
    ``{"fields": {field: [gate]}, "sections": [{id, gate, types}]}``,
    with gates in the order of the format, sections named ``<addon>.<id>``, fields without a gate omitted."""
    gates = ("requirements", "plan", "verify", "code")
    fields = {name: sorted(f["gate"], key=gates.index) for name, f in sorted(m.fields.items()) if f.get("gate")}
    sections = [
        {"id": f"{m.name}.{s['id']}", "gate": sorted(s["gate"], key=gates.index), "types": sorted(s["types"])}
        for s in sorted(m.sections, key=lambda s: s["id"])
        if s.get("gate")
    ]
    return {"fields": fields, "sections": sections}
