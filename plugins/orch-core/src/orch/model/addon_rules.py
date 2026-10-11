"""What a grant allows (ticket-format §8.1): the one set of rules that replay (``edits``, ``tasks``) and the host's
registry (``orch.addons.registry``) share.

The authority is the **grant** (``Addon.fields``, ``sections``, ``artifact_kinds``, as signed in ``addon.granted``),
never a manifest. All functions are pure and total: they return a :class:`Refusal` or ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

from orch.canon import is_clean_text

from .codes import Code, Refusal
from .types import Addon

__all__ = [
    "check_artifact_kind",
    "check_field_write",
    "check_section",
    "check_value",
    "holds_value",
    "validate_value",
]

_PERSON = re.compile(r"p_[0-9a-f]{32}(?![\s\S])")
_MAX_INT = 2**53 - 1


def validate_value(spec: Mapping[str, Any], value: Any) -> None:
    """Raise ``ValueError`` unless ``value`` is valid for the declared field ``spec`` (§8 table). ``None`` clears."""
    if value is None:
        return
    t = spec["type"]
    if t in ("string", "text"):
        limit = spec.get("max_len", 200 if t == "string" else 4096)
        if type(value) is not str or not value:
            raise ValueError("a non-empty string")
        if len(value) > limit or len(value.encode("utf-8")) > 4096:
            raise ValueError(f"longer than {limit}")
        if not is_clean_text(value, one_line=(t == "string")):
            raise ValueError("breaks the text rules")
    elif t == "integer":
        if type(value) is not int or abs(value) > _MAX_INT:
            raise ValueError("an integer")
        if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
            raise ValueError("outside min and max")
    elif t == "boolean":
        if type(value) is not bool:
            raise ValueError("true or false")
    elif t == "enum":
        if type(value) is not str or value not in spec["values"]:
            raise ValueError("one of the declared values")
    elif t == "string_list":
        if type(value) is not list or len(value) > spec.get("max_items", 1000):
            raise ValueError("a list within max_items")
        for x in value:
            if type(x) is not str or not x or len(x) > 200 or not is_clean_text(x, one_line=True):
                raise ValueError("items are one clean line of at most 200 characters")
        if len(set(value)) != len(value):
            raise ValueError("duplicate items")
    elif t == "person":
        if type(value) is not str or not _PERSON.fullmatch(value):
            raise ValueError("a person id")
    else:
        raise ValueError("unknown field type")


def holds_value(spec: Mapping[str, Any] | None, value: Any) -> bool:
    """A stored value is still valid under a (new) declaration; a replayed state holds only such values."""
    if spec is None:
        return False
    try:
        validate_value(spec, _thaw(value))
    except ValueError:
        return False
    return True


def _thaw(v: Any) -> Any:
    if isinstance(v, tuple | list):
        return [_thaw(x) for x in v]
    return v


def check_value(addon: Addon, fname: str, value: Any) -> Refusal | None:
    spec = addon.fields.get(fname)
    if spec is None:
        return Refusal(Code.ADDON_FIELD_UNKNOWN, f"{addon.name} declares no field {fname}")
    try:
        validate_value(spec, value)
    except ValueError as e:
        return Refusal(Code.ADDON_VALUE_INVALID, f"{addon.name}.{fname}: {e}")
    return None


def check_field_write(
    addon: Addon, fname: str, value: Any, actor: Mapping[str, Any], tokens: Collection[str] = ()
) -> Refusal | None:
    """May ``actor`` set ``ticket.addons.<addon>.<fname>`` to ``value``? ``set_by`` is a list of alternatives:
    ``agent`` (an agent with a grant), ``addon`` (that addon only) or a human token (a person who holds it on the
    ticket, ``tokens``); the host never writes an addon field. The caller has checked that the addon is active."""
    spec = addon.fields.get(fname)
    if spec is None:
        return Refusal(Code.ADDON_FIELD_UNKNOWN, f"{addon.name} declares no field {fname}")
    allowed = set(spec["set_by"])
    kind = actor.get("kind")
    if kind == "agent":
        ok = "agent" in allowed and bool(actor.get("grant"))
    elif kind == "addon":
        ok = "addon" in allowed and actor.get("id") == addon.name
    elif kind == "person":
        ok = bool(allowed & set(tokens) - {"agent", "addon"})
    else:
        ok = False
    if not ok:
        return Refusal(Code.ROLE_DENIED, f"{addon.name}.{fname} is not settable by this actor (set_by)")
    return check_value(addon, fname, value)


def check_section(addon: Addon, section_id: str, ticket_type: str) -> Refusal | None:
    for s in addon.sections:
        if s["id"] == section_id and ticket_type in s["types"]:
            return None
    return Refusal(Code.BODY_UNKNOWN_SECTION, section_id)


def check_artifact_kind(addon: Addon, kind: str) -> Refusal | None:
    if kind not in addon.artifact_kinds:
        return Refusal(Code.ARTIFACT_KIND, f"{addon.name} declares no artifact kind {kind!r}")
    return None
