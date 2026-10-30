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
from orch.addons.package import MANIFEST_FILE, Package, PackageError, check_path

__all__ = [
    "ADDON_API",
    "CAPABILITIES",
    "LEGACY_API",
    "Manifest",
    "ManifestError",
    "RUNTIME_COMMANDS",
    "declarations",
    "incompatibility",
    "load_manifest",
    "load_package_manifest",
]

ADDON_API = "1.2"  # the add-on API this core implements, major.minor (docs/architecture/addon-api.md)
LEGACY_API = ">=1.0,<2"  # what an orch.addon/2 manifest (no api key) is read as

# the closed list (§11.4; addon-api 1.1 adds choose_model and credential; credential needs network)
CAPABILITIES = ("serve_http", "spawn_agent", "pty", "network", "git_push", "choose_model", "credential")
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
    def api(self) -> str:
        """The api range the add-on needs; an ``orch.addon/2`` manifest has none and is read as :data:`LEGACY_API`."""
        return self.doc.get("api", LEGACY_API)

    @property
    def min_core(self) -> str | None:
        return self.doc.get("min_core")

    @property
    def runtime(self) -> dict[str, str] | None:
        """``{kind, lock}``: the one lockfile of the add-on's dependencies (spec 8.1), or ``None``."""
        rt = self.doc.get("runtime")
        return None if rt is None else dict(rt)

    @property
    def settings(self) -> dict[str, Any] | None:
        """``{title, schema}`` of the settings the person can configure (addon-api 1.1), or ``None``."""
        return self.doc.get("settings")

    @property
    def events(self) -> list[dict[str, Any]]:
        """The wake events the add-on can report (addon-api 1.2), as written."""
        return self.doc.get("events", [])

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
    if BARE_PYTHON.fullmatch(doc["entry"]["cmd"][0]):
        # python is never taken from PATH (V3 G6/G4, no legacy): the command the owner reads is the one that runs
        raise ManifestError("orch-addon.json/entry/cmd: use {python}, never a python found on PATH")
    rt = doc.get("runtime")
    if rt is not None:
        _check_runtime(doc, rt)
    return Manifest(doc)


#: a bare interpreter name would be looked up on PATH; case-insensitive (the file system may be), one pattern for the
#: manifest and the runner
BARE_PYTHON = re.compile(r"python[0-9.]*t?", re.I)
#: ``runtime.kind`` -> the interpreter the entry command must name (design G4 §2): the command is what the owner
#: reads, and the runner starts exactly the interpreter the dependencies were chosen for
RUNTIME_COMMANDS = {"python-requirements": "{python}", "node-package-lock": "node"}


def _check_runtime(doc: dict[str, Any], rt: dict[str, Any]) -> None:
    try:
        check_path(rt["lock"])
    except PackageError as e:
        raise ManifestError(f"orch-addon.json/runtime/lock: {e}") from None
    want = RUNTIME_COMMANDS[rt["kind"]]
    if doc["entry"]["cmd"][0] != want:
        raise ManifestError(f"orch-addon.json/runtime: a {rt['kind']} add-on starts with {want!r}")


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
        d = {"id": f"{m.name}.{s['id']}", "types": sorted(s["types"]), "heading_key": schema.heading_key(s["heading"])}
        if s.get("gate"):
            d["gate"] = sorted(s["gate"], key=_GATES.index)
        sections.append(d)
    out: dict[str, Any] = {
        "fields": fields,
        "sections": sections,
        "artifact_kinds": sorted(k["kind"] for k in m.artifact_kinds),
    }
    if m.events:  # only when declared: a grant without events keeps its bytes
        out["events"] = [
            {
                "kind": e["kind"],
                "title": e["title"],
                "statuses": sorted(e["statuses"]),
                "ref": e["ref"]["format"],
                "interval_s": e["poll"]["interval_s"],
            }
            for e in sorted(m.events, key=lambda e: e["kind"])
        ]
    return out


# ------------------------------------------------------------------------------------------------ compatibility

_CLAUSE = re.compile(r"(>=|<=|==|>|<)([0-9]{1,4})(?:\.([0-9]{1,4}))?(?![\s\S])")
_CORE = re.compile(r"([0-9]{1,4})(?:\.([0-9]{1,4}))?(?:\.([0-9]{1,4}))?(?:\.dev([0-9]{1,6}))?(?![\s\S])")


def _api_tuple(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"([0-9]{1,4})\.([0-9]{1,4})", text)
    return (int(m[1]), int(m[2])) if m else None


def _core_key(text: str) -> tuple[int, int, int, int, int] | None:
    """Sort key of a core version: a ``.devN`` build sorts before the release it leads up to."""
    m = _CORE.fullmatch(text)
    if m is None:
        return None
    rel = (int(m[1]), int(m[2] or 0), int(m[3] or 0))
    return (*rel, 0, int(m[4])) if m[4] is not None else (*rel, 1, 0)


def _api_in_range(api: tuple[int, int], rng: str) -> bool | None:
    """True/False, or ``None`` when ``rng`` is not a valid range (the caller fails closed)."""
    clauses = rng.split(",")
    if not 1 <= len(clauses) <= 4:
        return None
    ok = True
    for c in clauses:
        m = _CLAUSE.fullmatch(c)
        if m is None:
            return None
        bound = (int(m[2]), int(m[3] or 0))
        ok = (
            ok
            and {">=": api >= bound, "<=": api <= bound, "==": api == bound, ">": api > bound, "<": api < bound}[m[1]]
        )
    return ok


def incompatibility(m: Manifest, core_version: str, api: str = ADDON_API) -> str | None:
    """Why ``m`` does not fit this core, or ``None`` when it does. Fails closed: a range or version that cannot be
    parsed, or a core version that cannot be parsed while ``min_core`` is set, is incompatible."""
    have = _api_tuple(api)
    ok = None if have is None else _api_in_range(have, m.api)
    if ok is None:
        return "the api range is not understood"
    if not ok:
        return f"needs add-on API {m.api}, this core has {api}"
    if m.min_core is not None:
        need, core = _core_key(m.min_core), _core_key(core_version)
        if need is None or core is None:
            return "the core version is not understood"
        if core < need:
            return f"needs orch core {m.min_core} or newer, this is {core_version}"
    return None
