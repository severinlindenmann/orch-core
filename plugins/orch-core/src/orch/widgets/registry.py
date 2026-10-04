"""Core types and templates. A core type is one module in `orch/widgets/types/` (found by listing the folder, so a
new type is a new file): NAME, SCHEMA (the type's own keys: a JSON Schema object with properties/required),
MOMENT, EXAMPLE (a whole block), render_html(block, ctx) -> Markup, render_text(block, ctx) -> str; optional ALT
("numbers" or "text": the toggle's label). Templates are folders with a widget.json (docs/widgets.md, "Templates")."""
from __future__ import annotations

import importlib
import json
import pkgutil
from functools import cache
from pathlib import Path
from types import ModuleType

MOMENTS = ("understand", "decide", "plan", "verify", "review", "debug", "report")
BUILTIN_TEMPLATES = Path(__file__).with_name("builtin")
COMMON = {
    "title": {"type": "string", "maxLength": 200},
    "source": {"type": "string", "maxLength": 500},
    "caption": {"type": "string", "maxLength": 500},
    "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"},  # a letter first: never mistaken for an index
}
_HEIGHT = {"type": "integer", "minimum": 40, "maximum": 2000}
_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
# `sha256` pins the template version the block was written against (`template_digest`): a template edited in place
# is drift, never drawn, and the verdict hash binds the pin (orch.core.artifacts.binding). `orch widget add` fills it.
WIDGET_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["widget", "sha256"], "properties": {
    **COMMON, "widget": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,39}@[1-9][0-9]{0,3}$"}, "sha256": _SHA,
    "data": {"type": "object"}, "height": _HEIGHT}}
HTML_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["html", "sha256"], "properties": {
    **COMMON, "html": {"type": "string", "pattern": "^(artifacts/[^/]+/|artifact:|_files/).+\\.html?$", "maxLength": 500}, "sha256": _SHA,
    "data": {"type": "object"}, "libs": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 10},
    "height": _HEIGHT}}


@cache
def core_types() -> dict[str, ModuleType]:
    from orch.widgets import types as pkg
    out = {}
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        out[mod.NAME] = mod
    return out


@cache
def block_schema(name: str) -> dict:
    """The whole block's schema for core type `name`: the common keys, `type`, and the type's own keys."""
    own = core_types()[name].SCHEMA
    return {"type": "object", "additionalProperties": False, "required": ["type", *own.get("required", [])],
            "properties": {**COMMON, "type": {"const": name}, **own.get("properties", {})}}


def _template_dirs(home: Path | None) -> list[Path]:
    """Built-ins first, then the workspace's own (orchestrator/widgets/), which shadow a built-in of the same name."""
    return [BUILTIN_TEMPLATES] + ([home / "widgets"] if home is not None else [])


# widget.json (docs/widgets.md, "Templates"): a template whose file does not have this shape is left out and named
# in `template_problems` (orch widget list/check, /widgets), so a typo never turns into a 500 on a ticket page.
TEMPLATE_SCHEMA = {"type": "object", "required": ["versions"], "properties": {
    "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,39}$"},
    "title": {"type": "string", "maxLength": 200}, "description": {"type": "string", "maxLength": 4000},
    "moment": {"type": "string", "maxLength": 40},
    "libs": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 10},
    "min_height": _HEIGHT,
    "versions": {"type": "object", "minProperties": 1, "propertyNames": {"pattern": "^[1-9][0-9]{0,3}$"},
                 "additionalProperties": {"type": "object", "properties": {
                     "notes": {"type": "string", "maxLength": 4000}, "schema": {"type": "object"}}}}}}


@cache
def _load(path: str, size: int, mtime_ns: int) -> tuple[dict | None, str | None]:
    """(widget.json, None) or (None, why it is left out); read once per file version."""
    import jsonschema
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"not readable JSON: {e}"
    errors = sorted(jsonschema.Draft202012Validator(TEMPLATE_SCHEMA).iter_errors(spec), key=lambda e: list(map(str, e.path)))
    if errors:
        return None, "; ".join(f"{'/'.join(map(str, e.path)) or 'widget.json'}: {e.message}" for e in errors[:3])
    for v, entry in spec["versions"].items():
        try:
            jsonschema.Draft202012Validator.check_schema(entry.get("schema") or {})
        except jsonschema.SchemaError as e:
            return None, f"versions/{v}/schema is not a JSON Schema: {e.message}"
    return spec, None


def _scan(home: Path | None) -> tuple[dict[str, dict], list[dict]]:
    out: dict[str, dict] = {}
    bad: list[dict] = []
    for base in _template_dirs(home):
        for path in sorted(base.glob("*/widget.json")) if base.is_dir() else []:
            try:
                st = path.stat()
            except OSError:
                continue
            spec, why = _load(str(path), st.st_size, st.st_mtime_ns)
            origin = "builtin" if base == BUILTIN_TEMPLATES else "workspace"
            if spec is None:
                bad.append({"name": path.parent.name, "origin": origin, "path": str(path), "problem": why})
                continue
            name = str(spec.get("name") or path.parent.name)
            out[name] = {**spec, "name": name, "folder": str(path.parent), "origin": origin}
    return out, bad


def templates(home: Path | None = None) -> dict[str, dict]:
    """{name: widget.json plus "origin" (builtin|workspace) and "folder"}; malformed ones are left out."""
    return _scan(home)[0]


def template_problems(home: Path | None = None) -> list[dict]:
    """The templates left out: [{name, origin, path, problem}]."""
    return _scan(home)[1]


def template_version(home: Path | None, ref: str) -> tuple[dict | None, dict | None]:
    """(template, its version entry) for "name@v"; (None, None) or (template, None) when either is unknown."""
    name, _, version = ref.partition("@")
    spec = templates(home).get(name)
    return spec, (spec or {}).get("versions", {}).get(version)


def template_body(spec: dict, ref: str) -> bytes:
    """The bytes of a template version's page (`v<n>.html`); OSError when it cannot be read."""
    return (Path(spec["folder"]) / f"v{ref.partition('@')[2]}.html").read_bytes()


def template_digest(spec: dict, body: bytes) -> str:
    """What a block pins of a template version: the sha256 of its page bytes and the libraries it loads (widget.json
    `libs`), so neither can change under a pinned block."""
    import hashlib
    libs = json.dumps(sorted(str(x) for x in spec.get("libs", [])), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body + b"\0" + libs).hexdigest()


def template_state(home: Path | None, ref: str, pin) -> tuple[str, str | None]:
    """("ok" | "drift" | "missing", the current digest or None) of template version `ref` against the block's pin."""
    spec, version = template_version(home, ref)
    if version is None:
        return "missing", None
    try:
        current = template_digest(spec, template_body(spec, ref))
    except OSError:
        return "missing", None
    return ("ok" if pin == current else "drift"), current


def describe() -> list[dict]:
    """The core types for `orch widget types`."""
    return [{"name": m.NAME, "layer": "core", "moment": m.MOMENT, "schema": block_schema(m.NAME),
             "example": m.EXAMPLE} for m in core_types().values()]
