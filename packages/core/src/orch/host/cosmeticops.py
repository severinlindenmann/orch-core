"""The workspace's cosmetic settings, host side (run 13, D6): ``workspace.cosmetic.set {name?, icon?, if_current}``.

The name and the icon are display only. They are **not signed**: they live in ``config.json`` (``workspace.name``,
``workspace.icon``) and no event carries them. So the host protects them differently:

* a person only (column P) and the workspace owner; never an agent, a grant or a relay (the dashboard adds that the
  session must be a full one);
* ``if_current`` must equal what ``config.json`` holds now (a compare-and-set under the store lock), so an Undo never
  overwrites a later change (``cosmetic.conflict``);
* the name is one line, at most 80 characters, with no hidden, control or bidirectional characters; the icon is a pair
  of the closed lists of :mod:`orch.wsicons`;
* at most 10 changes a minute;
* every change writes one security-log line (person, old, new).
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch.wsicons import COLOUR_LABELS, ICON_LABELS

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["COSMETIC_OPS", "serve"]

COSMETIC_OPS: dict[str, int] = {"workspace.cosmetic.set": 1}
NAME_MAX = 80
RATE = 10  # changes per minute and workspace

_ICON = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"icon": {"enum": list(ICON_LABELS)}, "colour": {"enum": list(COLOUR_LABELS)}},
            "required": ["icon", "colour"],
            "additionalProperties": False,
        },
        {"type": "null"},
    ]
}
_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": NAME_MAX},
        "icon": _ICON,
        "if_current": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "icon": _ICON},
            "required": ["name", "icon"],
            "additionalProperties": False,
        },
    },
    "required": ["if_current"],
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(_SCHEMA)
_BAD = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def plain_name(name: str) -> bool:
    """One visible line: no control, format (zero-width, bidirectional), separator-line or unassigned characters."""
    return (
        name == name.strip()
        and bool(name)
        and len(name) <= NAME_MAX
        and not any(unicodedata.category(c) in _BAD for c in name)
    )


def _check(call: Call) -> dict[str, Any]:
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATOR.iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError(
            "invalid.input",
            f"workspace.cosmetic.set: {'.'.join(map(str, errors[0].absolute_path)) or 'args'} is not valid",
        )
    if "name" not in args and "icon" not in args:
        raise HostError("invalid.input", "workspace.cosmetic.set: give a name or an icon")
    if call.frame.get("files"):
        raise HostError("blob.invalid", "workspace.cosmetic.set takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", "dry_run is not supported by workspace.cosmetic.set")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    if call.column.name != "P" or call.viewer is None or call.relay is not None:
        raise HostError("op.not_allowed", "a person who owns the workspace changes its name and icon")
    member = service.store.state.workspace.members.get(call.viewer)
    if member is None or member.role != "owner":
        raise HostError("role.denied", "only the workspace owner changes its name and icon")
    if "name" in args and not plain_name(args["name"]):
        raise HostError("invalid.input", "workspace.cosmetic.set: the name has hidden or control characters")
    return args


def _rate(service: Any) -> None:
    now = service.clock()
    hits = [t for t in getattr(service, "_cosmetic_hits", []) if now - t < 60]
    if len(hits) >= RATE:
        raise HostError("host.busy", "too many name and icon changes: wait a minute", retry_after_ms=60_000)
    hits.append(now)
    service._cosmetic_hits = hits  # noqa: SLF001


def serve(call: Call) -> dict[str, Any]:
    from . import securitylog
    from .workspace import ok_result

    args = _check(call)
    service = call.service
    store = service.store
    want = {k: args[k] for k in ("name", "icon") if k in args}
    # the rate check runs under the store lock, with the compare-and-set: two racing calls count one after the other
    old, new = store.set_cosmetic(want, args["if_current"], before=lambda: _rate(service))
    if old is None:
        raise HostError("cosmetic.conflict", "the name or icon changed meanwhile: look again", detail={"current": new})
    securitylog.record(
        service.state_dir,
        service.host_uid,
        service.clock(),
        "workspace.cosmetic.set",
        workspace_id=service.workspace_id,
        principal=call.principal,
        person=call.viewer,
        old=old,
        new=new,
    )
    return ok_result(new)
