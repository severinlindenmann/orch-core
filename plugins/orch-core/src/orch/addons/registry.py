"""Registration from granted manifests: headings, needs rules, proposals, inactive data (ticket-format §8, §8.1).

The registry is **pure**: it is built from what the log says (the replayed ``Addon`` records: the signed
declarations) and from manifests the caller has already read and digest-checked, and it answers questions. It never
opens a file and never appends.

**The grant is the authority, the manifest only presentation.** Which fields exist, their types and limits,
``set_by``, the sections and their ticket types and the artifact kinds come from the grant (``Addon.fields`` and
friends, shared with replay in :mod:`orch.model.addon_rules`); the manifest supplies what the grant does not carry:
headings and places, ``needs`` rules, ``agents_md`` and ``entry``. An addon registers only while it is **active**:
granted, enabled, not purged, and the package it was loaded from has the digest of the grant. Every other state is
named (:func:`state_of`); its data is shown as inactive and never counted.

P1: tested, not yet called, except ``orch addon list`` (``state_of``). P2 wires ``check_write`` and
``check_section_write`` in front of every addon-path append (replay already enforces the same rules through
``orch.model.edits``), ``sections_for`` into the body renderer and parser, and ``check_proposal`` after
``runner.call``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from orch.addons.manifest import Manifest
from orch.canon import is_clean_text
from orch.model import addon_rules
from orch.model.addon_rules import validate_value
from orch.model.types import Addon
from orch.schema import MAX_SECTION_BYTES, _forged_heading, heading_key

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
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
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
    def __init__(self, addons: Mapping[str, Addon], manifests: Mapping[str, tuple[Manifest, str]]) -> None:
        """``addons``: name -> replayed ``Addon``. ``manifests``: name -> ``(manifest, digest of the package it was
        read from)``."""
        self._grants: dict[str, Addon] = {}
        self._manifests: dict[str, Manifest] = {}
        self._state: dict[str, str] = {}
        for name, a in addons.items():
            loaded = manifests.get(name)
            self._state[name] = state = state_of(a, loaded)
            if state == ACTIVE and loaded is not None:
                self._grants[name] = a
                self._manifests[name] = loaded[0]

    # -- states
    def state(self, name: str) -> str:
        return self._state.get(name, "unknown")

    def active(self) -> list[str]:
        return sorted(self._grants)

    def manifest(self, name: str) -> Manifest | None:
        return self._manifests.get(name)

    # -- registration (from the grant)
    def field_spec(self, addon: str, name: str) -> dict[str, Any] | None:
        a = self._grants.get(addon)
        return a.fields.get(name) if a else None

    def artifact_kinds(self, addon: str) -> list[str]:
        a = self._grants.get(addon)
        return list(a.artifact_kinds) if a else []

    # -- registration (from the manifest: presentation)
    def section_heading(self, section_id: str) -> str | None:
        """The heading of ``<addon>.<id>`` when its addon is active, else ``None``."""
        addon, _, sid = section_id.partition(".")
        m = self._manifests.get(addon)
        return next((s["heading"] for s in m.sections if s["id"] == sid), None) if m else None

    def sections_for(self, ticket_type: str) -> list[tuple[str, str, str]]:
        """``(section id, heading, after)`` of each active addon section for the ticket type, by addon then manifest."""
        out = []
        for n in sorted(self._grants):
            allowed = {s["id"]: set(s["types"]) for s in self._grants[n].sections}
            for s in self._manifests[n].sections:
                if ticket_type in allowed.get(f"{n}.{s['id']}", ()):
                    out.append((f"{n}.{s['id']}", s["heading"], s["after"]))
        return out

    def heading_conflicts(self, manifest: Manifest) -> list[str]:
        """Headings of ``manifest`` whose key (:func:`orch.schema.heading_key`) equals one of another active addon's
        (``orch addon grant`` refuses a collision, §8.1). Core headings are checked by the manifest schema."""
        taken = {
            heading_key(s["heading"]) for n, m in self._manifests.items() if n != manifest.name for s in m.sections
        }
        return [s["heading"] for s in manifest.sections if heading_key(s["heading"]) in taken]

    def needs_rules(self) -> list[tuple[str, dict[str, Any]]]:
        """``(addon, rule)`` of every active addon, in addon order then manifest order."""
        return [(n, r) for n in sorted(self._manifests) for r in self._manifests[n].needs]

    # -- writes
    def check_write(
        self,
        addon: str,
        fname: str,
        value: Any,
        actor: Mapping[str, Any],
        tokens: Collection[str] = (),
    ) -> None:
        """Raise :class:`WriteRefused` unless ``actor`` may set ``ticket.addons.<addon>.<fname>`` to ``value``: the same
        rules replay applies (:func:`orch.model.addon_rules.check_field_write`). ``tokens`` are the §5.9 tokens a
        person holds on the ticket."""
        a = self._grants.get(addon)
        if a is None:
            raise WriteRefused("addon.unknown", f"{addon} is not granted or not enabled")
        r = addon_rules.check_field_write(a, fname, value, actor, tokens)
        if r is not None:
            raise WriteRefused(r.code.value, r.detail)

    def check_section_write(self, section_id: str, ticket_type: str) -> None:
        a = self._grants.get(section_id.split(".", 1)[0])
        if a is None:
            raise WriteRefused("addon.unknown", f"{section_id.split('.', 1)[0]} is not granted or not enabled")
        r = addon_rules.check_section(a, section_id, ticket_type)
        if r is not None:
            raise WriteRefused(r.code.value, r.detail)

    # -- proposals
    def check_proposal(self, addon: str, ticket_type: str, result: Any) -> Proposal:
        """The host's validation of a ``propose`` result (§8.1). Raises :class:`ProposalError`."""
        a = self._grants.get(addon)
        if a is None:
            raise ProposalError(f"{addon} is not an active addon")
        if type(result) is not dict or set(result) - {"set", "sections", "artifacts"}:
            raise ProposalError("result: only set, sections and artifacts")
        sets, secs, arts = result.get("set", {}), result.get("sections", {}), result.get("artifacts", [])
        if type(sets) is not dict or type(secs) is not dict or type(arts) is not list:
            raise ProposalError("result: set and sections are objects, artifacts a list")
        out = Proposal(addon)
        for fname, value in sets.items():
            spec = a.fields.get(fname) if type(fname) is str else None
            if spec is None or "addon" not in spec["set_by"]:
                raise ProposalError(f"set: {str(fname)[:40]!r} is not a field this addon may set")
            r = addon_rules.check_value(a, fname, value)
            if r is not None:
                raise ProposalError(f"set.{fname}: {r.detail}")
            out.set[f"ticket.addons.{addon}.{fname}"] = value
        for sid, text in secs.items():
            qualified = f"{addon}.{sid}" if type(sid) is str else ""
            if addon_rules.check_section(a, qualified, ticket_type) is not None:
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
            out.sections[qualified] = text
        if len(arts) > _MAX_ITEMS:
            raise ProposalError(f"artifacts: more than {_MAX_ITEMS}")
        for x in arts:
            if type(x) is not dict or set(x) != {"kind", "name", "ref"}:
                raise ProposalError("artifacts: each is {kind, name, ref}")
            if type(x["kind"]) is not str or addon_rules.check_artifact_kind(a, x["kind"]) is not None:
                raise ProposalError("artifacts: a kind this addon did not declare")
            name = x["name"]
            if type(name) is not str or not 0 < len(name) <= 128 or not name[0].isalnum() or set(name) - _NAME_CHARS:
                raise ProposalError("artifacts: name is a plain file name")
            ref = x["ref"]
            if type(ref) is not str or not ref or len(ref) > _MAX_REF or not is_clean_text(ref, one_line=True):
                raise ProposalError("artifacts: ref is one clean line")
            out.artifacts.append({"kind": x["kind"], "name": name, "ref": ref})
        return out


# ---------------------------------------------------------------------------------------------------- inactive data


def view_data(registry: Registry, addons_data: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """How a ticket's ``addons`` object is shown: ``{addon: {"state": …, "active": bool, "fields": {…}}}``. Data of an
    addon that is not active (disabled, purged, changed, missing, unknown) is still returned, marked ``active: False``;
    a caller must never add it to a total, a filter or a waiting list. Addon artifacts (``addon`` + ``ref``) follow
    the same rule: shown inactive after a disable."""
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(addons_data):
        state = registry.state(name)
        out[name] = {"state": state, "active": state == ACTIVE, "fields": dict(addons_data[name])}
    return out
