"""JSON Schemas for the v2 format (JSON Schema 2020-12), with a small loader and validator.

The schemas live as ``.json`` files in ``schemas/`` (package data). Names are the file stems, for example
``ticket``, ``workspace``, ``event``, ``event.gate.approved`` or ``addon-manifest``.

* ``names()``: every schema name.
* ``load(name)``: the schema as a dict (a copy).
* ``validate(name, obj)``: raises :class:`SchemaError` (with a JSON pointer) on the first problem. Besides the
  schema it enforces the rules JSON Schema cannot express: NFC text, LF line endings, no floats, size limits
  and a few cross-field checks (ids unique, ``proves`` points at real criteria, ...).
* ``validate("event", obj)`` dispatches on ``obj["type"]``: core types use ``event.<type>``; ``<addon>.*``
  types are checked against the envelope only.
* ``parse_json(text)``: ``json.loads`` that refuses duplicate keys (format doc section 3).
"""

from __future__ import annotations

import json
import unicodedata
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

__all__ = ["SchemaError", "load", "names", "parse_json", "validate"]

BASE = "https://schemas.orch.dev/v2/"
MAX_SECTION_BYTES = 64 * 1024
MAX_TICKET_BYTES = 256 * 1024


class SchemaError(ValueError):
    """A document does not match its schema. ``path`` is an RFC 6901 JSON pointer ("" is the root)."""

    def __init__(self, schema: str, path: str, message: str) -> None:
        super().__init__(f"{schema}: {path or '/'}: {message}")
        self.schema = schema
        self.path = path
        self.message = message


def _pointer(parts: Any) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


@cache
def _files() -> dict[str, Any]:
    root = resources.files(__package__).joinpath("schemas")
    return {p.name[: -len(".json")]: p for p in root.iterdir() if p.name.endswith(".json")}


def names() -> list[str]:
    """All schema names, sorted."""
    return sorted(_files())


@cache
def _raw(name: str) -> dict[str, Any]:
    try:
        path = _files()[name]
    except KeyError:
        raise KeyError(f"unknown schema {name!r}") from None
    return json.loads(path.read_text(encoding="utf-8"))


def load(name: str) -> dict[str, Any]:
    """Return a copy of the named schema."""
    return json.loads(json.dumps(_raw(name)))


@cache
def _registry() -> Registry:
    reg: Registry = Registry()
    for n in names():
        reg = reg.with_resource(BASE + n, Resource.from_contents(_raw(n), default_specification=DRAFT202012))
    return reg


@cache
def _validator(name: str) -> Draft202012Validator:
    _raw(name)
    return Draft202012Validator({"$ref": BASE + name}, registry=_registry())


def parse_json(text: str) -> Any:
    """``json.loads`` that raises ``ValueError`` on duplicate object keys."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k in out:
                raise ValueError(f"duplicate key {k!r}")
            out[k] = v
        return out

    return json.loads(text, object_pairs_hook=pairs)


def _walk_text(name: str, value: Any, parts: tuple[Any, ...] = ()) -> None:
    """Text rules (format doc section 3): NFC, LF only, no floats."""
    if isinstance(value, float):
        raise SchemaError(name, _pointer(parts), "floats are not allowed (use integers such as ms or cents)")
    if isinstance(value, str):
        if "\r" in value:
            raise SchemaError(name, _pointer(parts), "text must use LF line endings")
        if not unicodedata.is_normalized("NFC", value):
            raise SchemaError(name, _pointer(parts), "text must be NFC-normalised")
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk_text(name, k, (*parts, k))
            _walk_text(name, v, (*parts, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk_text(name, v, (*parts, i))


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _check_ticket(obj: dict[str, Any]) -> None:
    if _size(obj) > MAX_TICKET_BYTES:
        raise SchemaError("ticket", "", f"ticket.json exceeds {MAX_TICKET_BYTES} bytes")
    for field in ("acceptance", "tasks", "questions"):
        seen: set[str] = set()
        for i, x in enumerate(obj[field]):
            if x["id"] in seen:
                raise SchemaError("ticket", _pointer([field, i, "id"]), f"duplicate id {x['id']!r}")
            seen.add(x["id"])
    acs = {a["id"] for a in obj["acceptance"]}
    for i, t in enumerate(obj["tasks"]):
        for j, ac in enumerate(t["proves"]):
            if ac not in acs:
                raise SchemaError("ticket", _pointer(["tasks", i, "proves", j]), f"unknown acceptance criterion {ac!r}")
    for i, q in enumerate(obj["questions"]):
        keys = [o["key"] for o in q.get("options", [])]
        if len(set(keys)) != len(keys):
            raise SchemaError("ticket", _pointer(["questions", i, "options"]), "duplicate option key")
        if "recommended" in q and q["recommended"] not in keys:
            raise SchemaError("ticket", _pointer(["questions", i, "recommended"]), "recommended is not an option key")
    if obj["key"] in obj["blocked_by"] or obj["key"] == obj["parent"]:
        raise SchemaError("ticket", "/key", "a ticket cannot be its own parent or blocker")


def _check_body(obj: dict[str, Any]) -> None:
    for sid, text in obj["sections"].items():
        if len(text.encode("utf-8")) > MAX_SECTION_BYTES:
            raise SchemaError("body", _pointer(["sections", sid]), f"section exceeds {MAX_SECTION_BYTES} bytes")


def _first_error(name: str, obj: Any) -> None:
    errors = list(_validator(name).iter_errors(obj))
    if not errors:
        return
    from jsonschema.exceptions import best_match

    # An unevaluatedProperties error is a by-product when a $ref'd envelope fails; report the real cause instead.
    primary = [e for e in errors if e.validator != "unevaluatedProperties"] or errors
    err = best_match(primary) or primary[0]
    raise SchemaError(name, _pointer(err.absolute_path), err.message)


def validate(name: str, obj: Any) -> None:
    """Validate ``obj`` against the named schema or raise :class:`SchemaError`."""
    _walk_text(name, obj)
    if name == "event":
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
            _first_error("event", obj)
            return
        specific = "event." + obj["type"]
        if specific in _files():
            _first_error(specific, obj)
            return
        _first_error("event", obj)
        prefix = obj["type"].split(".", 1)[0]
        if "." not in obj["type"] or prefix in _core_prefixes():
            raise SchemaError("event", "/type", f"unknown core event type {obj['type']!r}")
        return
    _first_error(name, obj)
    if name == "ticket":
        _check_ticket(obj)
    elif name == "body":
        _check_body(obj)


@cache
def _core_prefixes() -> frozenset[str]:
    return frozenset(_raw("event")["$defs"]["coreTypePrefixes"]["enum"])
