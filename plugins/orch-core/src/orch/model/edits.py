"""Edits (ticket-format §5.8): ``ticket.updated`` with ``base_rev`` and ``edit.external``.

The model keeps definitions (``ticket.json`` values) and section hashes with their artifact references, never prose.
Bound paths (§5.7) are refused on ``done`` and ``closed`` tickets from every actor.
"""

from __future__ import annotations

import copy
from typing import Any

from orch.canon import value_hash
from orch.schema import SECTIONS_BY_TYPE

from . import generations, lifecycle
from .codes import Code, Refusal
from .gates import EMPTY
from .types import Core, TCore, WsCore

PROTECTED = frozenset({"ticket.schema", "ticket.uid", "ticket.key", "ticket.visibility", "ticket.questions"})  # §5.8


def _valid_section(ws: WsCore, ticket_type: str, sid: str) -> bool:
    if "." in sid:
        a = ws.addons.get(sid.split(".", 1)[0])
        return a is not None and a.enabled and not a.purged
    return sid in SECTIONS_BY_TYPE[ticket_type]


def _current_value(t: TCore, key: str) -> Any:
    if key.startswith("addons."):
        _, a, f = key.split(".", 2)
        return t.fields["addons"].get(a, {}).get(f)
    return t.fields[key]


def _check_refs(t: TCore, sections: dict[str, Any]) -> Refusal | None:
    for sid, v in sections.items():
        for name in (v or {}).get("refs", []):
            art = t.artifacts.get(name)
            if art is None or art.digest is None:
                return Refusal(Code.BODY_UNKNOWN_ARTIFACT, f"{sid} references unknown artifact {name}")
    return None


def _check_fields(core: Core, t: TCore, f: dict[str, Any], touched: set[str], e: dict[str, Any]) -> Refusal | None:
    """Cross-references of what this edit changes; an old value that went stale (a repo removed from the settings)
    never blocks an unrelated edit."""
    if touched & {"ticket.tasks", "ticket.acceptance"}:
        ac = {a["id"] for a in f["acceptance"]}
        for task in f["tasks"]:
            if not set(task["proves"]) <= ac:
                return Refusal(Code.TICKET_BAD_REFERENCE, f"{task['id']} proves an unknown acceptance criterion")
    if touched & {"ticket.parent", "ticket.blocked_by"}:
        for key in [x for x in [f["parent"], *f["blocked_by"]] if x is not None]:
            if not lifecycle.known_before(core, t, key, e):
                return Refusal(Code.TICKET_BAD_REFERENCE, f"unknown ticket key {key}")
    if "ticket.links" in touched:
        lk = f["links"]
        unknown = [r for r in lk["repos"] if r not in core.ws.repos]
        if unknown:
            return Refusal(Code.REPO_UNKNOWN, f"repos not in settings.repos: {unknown}")
        named = [*lk["branches"], *(p["repo"] for p in lk["prs"])]
        if any(r not in lk["repos"] for r in named):
            return Refusal(Code.TICKET_BAD_REFERENCE, "links.branches and links.prs name repos in links.repos")
    return None


def updated(core: Core, t: TCore, e: dict[str, Any]) -> Refusal | None:
    ws, sets, secs = core.ws, e.get("set", {}), e.get("sections", {})
    paths = set(sets) | {"body." + s for s in secs}
    new_type = sets.get("ticket.type", t.ticket_type)
    if t.status in ("done", "closed") and paths & generations.bound_any(ws, t.ticket_type):
        return Refusal(Code.TICKET_FROZEN, f"bound paths are refused on {t.status} tickets")
    for sid in secs:
        if not _valid_section(ws, new_type, sid):
            return Refusal(Code.BODY_UNKNOWN_SECTION, sid)
    for path in sets:
        if path in PROTECTED:
            return Refusal(Code.PATH_PROTECTED, path)
        key = path[len("ticket.") :]
        if key.startswith("addons."):  # §8.1: only a granted, enabled, not purged addon has live fields
            a = ws.addons.get(key.split(".")[1])
            if a is None or not a.enabled or a.purged:
                return Refusal(Code.ADDON_UNKNOWN, path)
    if (r := _check_refs(t, secs)) is not None:
        return r
    for path, h in e["base_rev"].items():
        if path.startswith("body."):
            cur = t.sections.get(path[5:], {}).get("hash", EMPTY)
        else:
            cur = value_hash(_current_value(t, path[len("ticket.") :]))
        if h != cur:
            return Refusal(Code.CONFLICT_SECTION, f"{path} changed since it was read")
    fields = copy.deepcopy(t.fields)
    for path, v in sets.items():
        key = path[len("ticket.") :]
        if key.startswith("addons."):
            _, a, f = key.split(".", 2)
            fields["addons"].setdefault(a, {})[f] = copy.deepcopy(v)
        else:
            fields[key] = copy.deepcopy(v)
    if (r := _check_fields(core, t, fields, set(sets), e)) is not None:
        return r
    t.fields = fields
    for sid, v in secs.items():
        if v is None:
            t.sections.pop(sid, None)
        else:
            t.sections[sid] = {"hash": v["hash"], "refs": list(v["refs"])}
    generations.mark_paths(ws, t, paths)
    return None


def external(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    secs = e["sections"]
    paths = {"body." + s for s in secs}
    if t.status in ("done", "closed") and paths & generations.bound_any(ws, t.ticket_type):
        return Refusal(Code.TICKET_FROZEN, "a bound section of a done or closed ticket is reverted, not installed")
    for sid in secs:
        if not _valid_section(ws, t.ticket_type, sid):
            return Refusal(Code.BODY_UNKNOWN_SECTION, sid)
    for sid, v in secs.items():
        if v is None:
            t.sections.pop(sid, None)
        else:
            t.sections[sid] = {"hash": v["hash"], "refs": list(v["refs"])}
    generations.mark_paths(ws, t, paths)
    return None
