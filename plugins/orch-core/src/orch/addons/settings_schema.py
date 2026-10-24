"""The closed JSON-Schema subset an add-on may use for its settings (addon-api 1.1, section 8).

The manifest's ``settings.schema`` is checked here when the manifest loads; the host validates a settings value against
it later (jsonschema 2020-12). Everything not named below is refused, so a schema cannot hide a regex, a reference or a
format check, and a person is shown every leaf of what they sign.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from orch.canon import suspicious

__all__ = ["MAX_DEPTH", "SettingsSchemaError", "check_settings_schema"]

MAX_DEPTH = 4  # nesting of schema nodes, the root is depth 1
_MAX_ENTRIES = 64  # properties, enum values, maxItems, maxProperties
_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}(?![\s\S])")
_TYPES = ("object", "string", "integer", "boolean", "array")
_COMMON = {"type", "title"}
_KEYS = {
    "object": _COMMON | {"properties", "additionalProperties", "required", "maxProperties"},
    "string": _COMMON | {"enum", "maxLength"},
    "integer": _COMMON | {"minimum", "maximum"},
    "boolean": _COMMON,
    "array": _COMMON | {"items", "maxItems"},
}
_SAFE_INT = 2**53 - 1


class SettingsSchemaError(ValueError):
    """The schema leaves the subset; ``path`` is where (a list of keys and indexes), the message says why."""

    def __init__(self, path: list[str | int], message: str) -> None:
        super().__init__(message)
        self.path = path


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _bounded(v: Any, lo: int, hi: int) -> bool:
    return _int(v) and lo <= v <= hi


def _text_ok(s: str) -> bool:
    """Printable, not only blank, no control/surrogate/private/unassigned character, no space but U+0020, nothing
    ``suspicious()`` flags (invisible, bidi, look-alikes, a leading mark)."""
    if not s.strip() or suspicious(s):
        return False
    return all(
        unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") and (c == " " or not c.isspace())
        for c in s
    )


def check_settings_schema(node: Any) -> None:
    """Raise :class:`SettingsSchemaError` unless ``node`` is a schema of the subset with an object at the root."""
    if not isinstance(node, dict) or node.get("type") != "object":
        raise SettingsSchemaError([], "the settings schema is an object")
    _node(node, [], 1)


def _node(node: Any, path: list[str | int], depth: int) -> None:
    if not isinstance(node, dict):
        raise SettingsSchemaError(path, "a schema is an object")
    if depth > MAX_DEPTH:
        raise SettingsSchemaError(path, f"nested deeper than {MAX_DEPTH}")
    typ = node.get("type")
    if typ not in _TYPES:
        raise SettingsSchemaError([*path, "type"], "type is one of object, string, integer, boolean, array")
    for k in node:
        if k not in _KEYS[typ]:
            raise SettingsSchemaError([*path, k], f"{k!r} is not allowed here")
    t = node.get("title")
    if t is not None and not (isinstance(t, str) and 1 <= len(t) <= 60 and _text_ok(t)):
        raise SettingsSchemaError([*path, "title"], "title is 1 to 60 printable characters")
    {"object": _object, "string": _string, "integer": _integer, "array": _array}.get(typ, lambda *_: None)(
        node, path, depth
    )


def _object(node: dict[str, Any], path: list[str | int], depth: int) -> None:
    if node.get("additionalProperties") is not False:
        raise SettingsSchemaError([*path, "additionalProperties"], "an object sets additionalProperties to false")
    props = node.get("properties", {})
    if not isinstance(props, dict) or len(props) > _MAX_ENTRIES:
        raise SettingsSchemaError([*path, "properties"], f"properties is an object of at most {_MAX_ENTRIES}")
    for k, sub in props.items():
        if not _KEY.fullmatch(k):
            raise SettingsSchemaError([*path, "properties", k], "a property name is lower-case letters, digits _ -")
        _node(sub, [*path, "properties", k], depth + 1)
    req = node.get("required", [])
    if (
        not isinstance(req, list)
        or len(set(map(str, req))) != len(req)
        or not all(isinstance(r, str) and r in props for r in req)
    ):
        raise SettingsSchemaError([*path, "required"], "required lists distinct names of properties")
    if "maxProperties" in node and not _bounded(node["maxProperties"], 0, _MAX_ENTRIES):
        raise SettingsSchemaError([*path, "maxProperties"], f"maxProperties is 0 to {_MAX_ENTRIES}")


def _string(node: dict[str, Any], path: list[str | int], depth: int) -> None:
    if "enum" not in node and "maxLength" not in node:
        raise SettingsSchemaError(path, "a string sets enum or maxLength")
    if "maxLength" in node and not _bounded(node["maxLength"], 1, 200):
        raise SettingsSchemaError([*path, "maxLength"], "maxLength is 1 to 200")
    if "enum" in node:
        e = node["enum"]
        if (
            not isinstance(e, list)
            or not 1 <= len(e) <= _MAX_ENTRIES
            or not all(isinstance(v, str) and 1 <= len(v) <= 64 and _text_ok(v) for v in e)
            or len(set(e)) != len(e)
        ):
            raise SettingsSchemaError(
                [*path, "enum"], f"enum is 1 to {_MAX_ENTRIES} distinct printable strings of at most 64 characters"
            )


def _integer(node: dict[str, Any], path: list[str | int], depth: int) -> None:
    if "minimum" not in node or "maximum" not in node:
        raise SettingsSchemaError(path, "an integer sets minimum and maximum")
    for k in ("minimum", "maximum"):
        if k in node and not _bounded(node[k], -_SAFE_INT, _SAFE_INT):
            raise SettingsSchemaError([*path, k], f"{k} is a whole number")
    if "minimum" in node and "maximum" in node and node["minimum"] > node["maximum"]:
        raise SettingsSchemaError([*path, "minimum"], "minimum is greater than maximum")


def _array(node: dict[str, Any], path: list[str | int], depth: int) -> None:
    if "items" not in node:
        raise SettingsSchemaError([*path, "items"], "an array names its items")
    _node(node["items"], [*path, "items"], depth + 1)
    if not _bounded(node.get("maxItems"), 0, _MAX_ENTRIES):
        raise SettingsSchemaError([*path, "maxItems"], f"an array sets maxItems, 0 to {_MAX_ENTRIES}")
