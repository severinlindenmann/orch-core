"""The addon manifest: load, validate, derive the grant's declarations (ticket-format §8, §8.1).

``load_manifest`` takes the bytes of ``orch-addon.json`` (from :func:`orch.addons.package.read_package`), parses them
with the strict JSON parser and validates them against the ``addon-manifest`` schema. ``declarations`` is the only
place that turns a manifest into the declarations of an ``addon.granted`` event; replay never looks at a manifest again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from orch import canon, schema
from orch.addons.package import MANIFEST_FILE, Package

__all__ = ["CAPABILITIES", "Manifest", "ManifestError", "declarations", "load_manifest", "load_package_manifest"]

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


_GATES = ("requirements", "plan", "verify", "code")


def declarations(m: Manifest) -> dict[str, Any]:
    """``fields``, ``sections`` and ``artifact_kinds`` of the ``addon.granted`` event (§5.4.2, §8.1), derived from the
    manifest: a field keeps its type, limits, ``set_by`` and ``gate`` (``show`` and ``filter`` are display hints and
    grant nothing); a section is named ``<addon>.<id>``; lists are sorted. Replay uses only this."""
    fields: dict[str, Any] = {}
    for name, f in sorted(m.fields.items()):
        d = {k: v for k, v in f.items() if k not in ("show", "filter")}
        d["set_by"] = sorted(d["set_by"])
        if "gate" in d:
            d["gate"] = sorted(d["gate"], key=_GATES.index)
        fields[name] = d
    sections = []
    for s in sorted(m.sections, key=lambda s: s["id"]):
        d = {"id": f"{m.name}.{s['id']}", "types": sorted(s["types"])}
        if s.get("gate"):
            d["gate"] = sorted(s["gate"], key=_GATES.index)
        sections.append(d)
    return {"fields": fields, "sections": sections, "artifact_kinds": sorted(k["kind"] for k in m.artifact_kinds)}
