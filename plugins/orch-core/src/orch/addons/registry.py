"""Registration from granted manifests: fields, sections, artifact kinds, ``set_by`` checks, proposals, inactive data
(ticket-format §8, §8.1).

The registry is **pure**: it is built from what the log says (the replayed ``Addon`` records) and from manifests the
caller has already read and digest-checked, and it answers questions. It never opens a file and never appends.

* An addon is **active** when it is granted, enabled, not purged and the package it was loaded from has the digest of
  the grant. Only an active addon registers anything; every other state is named (:func:`state_of`) and its data is
  shown as inactive, never counted.
* :meth:`Registry.check_write` is the ``set_by`` check per actor, plus the value check for the field's type.
* :meth:`Registry.check_proposal` validates what an addon process returned (§8.1 runner): it can only name its own
  fields (``set_by`` has ``addon``), its own sections of the ticket's type and its own artifact kinds.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from orch.addons.manifest import Manifest
from orch.canon import is_clean_text
from orch.schema import MAX_SECTION_BYTES, _forged_heading

__all__ = [
    "ACTIVE",
    "STATES",
    "Proposal",
    "ProposalError",
    "Registry",
    "WriteRefused",
    "state_of",
    "validate_value",
    "view_data",
]

ACTIVE = "active"
STATES = (ACTIVE, "disabled", "purged", "changed", "missing", "unknown")
_PERSON = re.compile(r"p_[0-9a-f]{32}(?![\s\S])")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?![\s\S])")
_MAX_INT = 2**53 - 1
_MAX_REF = 4096
_MAX_ITEMS = 32  # most artifacts in one proposal


class WriteRefused(ValueError):
    """A write is refused; ``code`` is a code of ticket-format §10.4a."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ProposalError(ValueError):
    """What an addon returned is not a valid proposal; nothing of it is used."""


def state_of(addon: Any, loaded: tuple[Manifest, str] | None) -> str:
    """One of :data:`STATES` for a replayed ``Addon`` (``None``: never granted) and ``(manifest, package digest)``
    as read from its package (``None``: no package on disk). ``changed``: not the package that was granted."""
    if addon is None:
        return "unknown"
    if addon.purged:
        return "purged"
    if not addon.enabled:
        return "disabled"
    if loaded is None:
        return "missing"
    manifest, digest = loaded
    if digest != addon.package_sha256 or manifest.version != addon.version or manifest.name != addon.name:
        return "changed"
    return ACTIVE


@dataclass(frozen=True)
class Proposal:
    """A validated proposal, in the host's terms: leaf paths (``ticket.addons.<addon>.<field>``) and section ids
    (``<addon>.<id>``). Nothing here is an event: the host builds and signs those."""

    addon: str
    set: dict[str, Any] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    artifacts: list[dict[str, str]] = field(default_factory=list)


class Registry:
    def __init__(self, addons: Mapping[str, Any], manifests: Mapping[str, tuple[Manifest, str]]) -> None:
        """``addons``: name -> replayed ``Addon`` (``version``, ``package_sha256``, ``enabled``, ``purged``).
        ``manifests``: name -> ``(manifest, digest of the package it was read from)``."""
        self._active: dict[str, Manifest] = {}
        self._state: dict[str, str] = {}
        for name, a in addons.items():
            loaded = manifests.get(name)
            self._state[name] = state = state_of(a, loaded)
            if state == ACTIVE and loaded is not None:
                self._active[name] = loaded[0]

    # -- states
    def state(self, name: str) -> str:
        return self._state.get(name, "unknown")

    def active(self) -> list[str]:
        return sorted(self._active)

    def manifest(self, name: str) -> Manifest | None:
        return self._active.get(name)

    # -- registration
    def field_spec(self, addon: str, name: str) -> dict[str, Any] | None:
        m = self._active.get(addon)
        return m.fields.get(name) if m else None

    def section_heading(self, section_id: str) -> str | None:
        """The heading of ``<addon>.<id>`` when its addon is active, else ``None``."""
        addon, _, sid = section_id.partition(".")
        m = self._active.get(addon)
        return next((s["heading"] for s in m.sections if s["id"] == sid), None) if m else None

    def sections_for(self, ticket_type: str) -> list[tuple[str, str, str]]:
        """``(section id, heading, after)`` of each active addon section for the ticket type, by addon then manifest."""
        return [
            (f"{n}.{s['id']}", s["heading"], s["after"])
            for n in sorted(self._active)
            for s in self._active[n].sections
            if ticket_type in s["types"]
        ]

    def artifact_kinds(self, addon: str) -> list[str]:
        m = self._active.get(addon)
        return [k["kind"] for k in m.artifact_kinds] if m else []

    def needs_rules(self) -> list[tuple[str, dict[str, Any]]]:
        """``(addon, rule)`` of every active addon, in addon order then manifest order."""
        return [(n, r) for n in sorted(self._active) for r in self._active[n].needs]

    # -- writes
    def check_write(
        self,
        addon: str,
        fname: str,
        value: Any,
        actor: Mapping[str, Any],
        tokens: Collection[str] = (),
    ) -> None:
        """Raise :class:`WriteRefused` unless ``actor`` may set ``ticket.addons.<addon>.<fname>`` to ``value``.

        ``actor`` is the event actor (§5.2). ``tokens`` are the §5.9 tokens a person holds on the ticket (the caller
        takes them from the replayed state); for an agent or an addon they are ignored. ``set_by`` is a list of
        alternatives, one matching is enough."""
        if self.state(addon) != ACTIVE:
            raise WriteRefused("addon.unknown", f"{addon} is not granted or not enabled")
        spec = self.field_spec(addon, fname)
        if spec is None:
            raise WriteRefused("addon.field_unknown", f"{addon} has no field {fname}")
        allowed = set(spec["set_by"])
        kind = actor.get("kind")
        if kind == "agent":
            ok = "agent" in allowed and bool(actor.get("grant"))
        elif kind == "addon":
            ok = "addon" in allowed and actor.get("id") == addon
        elif kind == "person":
            ok = bool(allowed & set(tokens) - {"agent", "addon"})
        else:  # the host writes no addon field of its own
            ok = False
        if not ok:
            raise WriteRefused("role.denied", f"{addon}.{fname} is not settable by this actor (set_by)")
        try:
            validate_value(spec, value)
        except ValueError as e:
            raise WriteRefused("invalid.input", f"{addon}.{fname}: {e}") from None

    def check_section_write(self, section_id: str, ticket_type: str) -> None:
        addon = section_id.split(".", 1)[0]
        if self.state(addon) != ACTIVE:
            raise WriteRefused("addon.unknown", f"{addon} is not granted or not enabled")
        m = self._active[addon]
        sid = section_id.partition(".")[2]
        sec = next((s for s in m.sections if s["id"] == sid), None)
        if sec is None or ticket_type not in sec["types"]:
            raise WriteRefused("body.unknown_section", section_id)

    # -- proposals
    def check_proposal(self, addon: str, ticket_type: str, result: Any) -> Proposal:
        """The host's validation of a ``propose`` result (§8.1). Raises :class:`ProposalError`."""
        m = self._active.get(addon)
        if m is None:
            raise ProposalError(f"{addon} is not an active addon")
        if type(result) is not dict or set(result) - {"set", "sections", "artifacts"}:
            raise ProposalError("result: only set, sections and artifacts")
        sets, secs, arts = result.get("set", {}), result.get("sections", {}), result.get("artifacts", [])
        if type(sets) is not dict or type(secs) is not dict or type(arts) is not list:
            raise ProposalError("result: set and sections are objects, artifacts a list")
        out = Proposal(addon)
        for fname, value in sets.items():
            spec = m.fields.get(fname) if type(fname) is str else None
            if spec is None or "addon" not in spec["set_by"]:
                raise ProposalError(f"set: {str(fname)[:40]!r} is not a field this addon may set")
            try:
                validate_value(spec, value)
            except ValueError as e:
                raise ProposalError(f"set.{fname}: {e}") from None
            out.set[f"ticket.addons.{addon}.{fname}"] = value
        for sid, text in secs.items():
            sec = next((s for s in m.sections if s["id"] == sid), None) if type(sid) is str else None
            if sec is None or ticket_type not in sec["types"]:
                raise ProposalError(f"sections: {str(sid)[:40]!r} is not a section of this addon for a {ticket_type}")
            if (
                type(text) is not str
                or len(text.encode("utf-8")) > MAX_SECTION_BYTES
                or not is_clean_text(text)
                or text.startswith("\n")
                or text.endswith("\n")
                or _forged_heading(text) is not None
            ):
                raise ProposalError(f"sections.{sid}: clean text, no edge LF, no heading of its own, within the limit")
            out.sections[f"{addon}.{sid}"] = text
        if len(arts) > _MAX_ITEMS:
            raise ProposalError(f"artifacts: more than {_MAX_ITEMS}")
        kinds = {k["kind"] for k in m.artifact_kinds}
        for a in arts:
            if type(a) is not dict or set(a) != {"kind", "name", "ref"}:
                raise ProposalError("artifacts: each is {kind, name, ref}")
            if a["kind"] not in kinds:
                raise ProposalError("artifacts: a kind this addon did not declare")
            if type(a["name"]) is not str or not _NAME.fullmatch(a["name"]):
                raise ProposalError("artifacts: name is a plain file name")
            ref = a["ref"]
            if type(ref) is not str or not ref or len(ref) > _MAX_REF or not is_clean_text(ref, one_line=True):
                raise ProposalError("artifacts: ref is one clean line")
            out.artifacts.append({"kind": a["kind"], "name": a["name"], "ref": ref})
        return out


# ---------------------------------------------------------------------------------------------------- values


def validate_value(spec: Mapping[str, Any], value: Any) -> None:
    """Raise ``ValueError`` unless ``value`` is valid for the field ``spec`` (§8 table). ``None`` clears a field."""
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
    else:  # pragma: no cover  (the schema has a closed list)
        raise ValueError("unknown field type")


# ---------------------------------------------------------------------------------------------------- inactive data


def view_data(registry: Registry, addons_data: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """How a ticket's ``addons`` object is shown: ``{addon: {"state": …, "active": bool, "fields": {…}}}``. Data of an
    addon that is not active (disabled, purged, changed, missing, unknown) is still returned, marked ``active: False``;
    a caller must never add it to a total, a filter or a waiting list."""
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(addons_data):
        state = registry.state(name)
        out[name] = {"state": state, "active": state == ACTIVE, "fields": dict(addons_data[name])}
    return out
