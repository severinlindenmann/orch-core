"""Addon ticket options: a yes/no choice an addon adds to every ticket (manifest `ticket_options`).

Core draws it where a human decides about a ticket (the new-ticket form, the approve card, the ticket page) and keeps the
value per ticket in the addon's own state folder (`state_dir/addons/<name>/ticket-options.json`, local to this machine,
never committed). The addon reads it (`ctx.ticket_option`) and acts on it; core knows nothing about what it means.

Who may set it. A human only: `set_value` refuses any actor that is not a human (and, like every human action, a
process under an agent harness). The one other writer is the addon itself, for its OWN options and only to relay a
choice the human made somewhere else (a paired phone): `relay_value`, reached through `ctx.ops()`. An agent has no way
to call either: the CLI path for a write is human-only, and agents can read values (`orch addon ticket-option list`).

Only enabled, trusted, loaded addons take part (`ws.addons`): an addon that is installed but not enabled or not trusted
draws nothing and cannot be written to.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from orch.addons.cache import cache_dir
from orch.core.fsutil import atomic_write_text
from orch.core.locks import lock
from orch.errors import UsageError, ValidationError

FILE = "ticket-options.json"
MAX_TICKETS = 5000


@dataclass(frozen=True)
class OptionView:
    addon: str
    addon_title: str
    id: str
    label: str
    help: str
    value: bool
    key: str  # "<addon>/<id>": the form value and CLI name


def path_of(ws, addon: str) -> Path:
    return cache_dir(ws, addon) / FILE


def _read(ws, addon: str) -> dict:
    try:
        data = json.loads(path_of(ws, addon).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _loaded(ws):
    try:
        return [la for la in ws.addons if la.manifest is not None and la.manifest.ticket_options]
    except Exception:
        return []


def declared(ws) -> list:
    """(loaded addon, TicketOption) for every option of an enabled, trusted addon, in addon name order."""
    return [(la, o) for la in _loaded(ws) for o in la.manifest.ticket_options]


def _stored(data: dict, ticket_id: str, option_id: str, default: bool) -> bool:
    row = data.get(ticket_id)
    value = row.get(option_id) if isinstance(row, dict) else None
    return value if isinstance(value, bool) else default


def value(ws, addon: str, ticket_id: str, option_id: str) -> bool:
    """The option's value on a ticket: its explicit value, else the addon's declared default. False for an option
    that is not declared by an enabled, trusted addon (nothing can be on for an addon that is off)."""
    for la, o in declared(ws):
        if la.name == addon and o.id == option_id:
            return _stored(_read(ws, addon), ticket_id, option_id, o.default)
    return False


def views(ws, ticket_id: str | None = None) -> list[OptionView]:
    """Every option of the enabled, trusted addons with this ticket's value (the default when `ticket_id` is None,
    as on the new-ticket form)."""
    out, cache = [], {}
    for la, o in declared(ws):
        data = cache.setdefault(la.name, _read(ws, la.name) if ticket_id else {})
        out.append(OptionView(la.name, la.manifest.title, o.id, o.label, o.help,
                              _stored(data, ticket_id, o.id, o.default) if ticket_id else o.default, f"{la.name}/{o.id}"))
    return out


def _check(ws, addon: str, option_id: str):
    for la, o in declared(ws):
        if la.name == addon and o.id == option_id:
            return la, o
    raise UsageError(f"no ticket option {addon}/{option_id}", hint="it must be declared by an enabled, trusted addon "
                                                                  "(orch addon ticket-option list)")


def _write(ws, ticket_id: str, addon: str, option_id: str, new: bool, actor) -> bool:
    la, o = _check(ws, addon, option_id)
    if not isinstance(new, bool):
        raise ValidationError("a ticket option is on or off")
    with lock(ws, f"ticket-options-{addon}"):
        data = _read(ws, addon)
        if _stored(data, ticket_id, option_id, o.default) == new:
            return False  # nothing to change, nothing to announce
        row = data.get(ticket_id) if isinstance(data.get(ticket_id), dict) else {}
        data[ticket_id] = {**row, option_id: new}
        if len(data) > MAX_TICKETS:  # a runaway file: drop the oldest-inserted tickets
            for k in list(data)[: len(data) - MAX_TICKETS]:
                del data[k]
        p = path_of(ws, addon)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(data, indent=1) + "\n")
        try:
            p.chmod(0o600)
        except OSError:
            pass
    from orch.core.events import append_event
    append_event(ws, ticket_id, "ticket.option", actor, {"addon": addon, "option": option_id, "value": new})
    return True


def set_value(ws, ticket_id: str, addon: str, option_id: str, new: bool, actor) -> bool:
    """A human sets an option on a ticket (dashboard, `orch addon ticket-option set`). Returns whether it changed.
    The event it appends reaches the addon like any other, so it can act on it."""
    from orch.core.lifecycle import require_human
    require_human(actor, "setting a ticket option")
    return _write(ws, ticket_id, addon, option_id, new, actor)


def relay_value(ws, ticket_id: str, addon: str, option_id: str, new: bool) -> bool:
    """The addon relays a choice its human made elsewhere (its own paired phone). Only that addon's own options; the
    event is recorded as the agent `addon:<name>`, so the addon's own event handler can tell it from a desktop change."""
    from orch.core.events import Actor
    via = f"addon:{addon}"
    return _write(ws, ticket_id, addon, option_id, new, Actor("agent", via, via))


def apply_form(ws, ticket_id: str, offered, on, actor) -> list[str]:
    """The dashboard's forms: `offered` are the option keys the form showed, `on` the ones ticked. Sets each offered
    option to its ticked state (an unticked box turns it off); unknown keys are ignored. Returns the keys that changed."""
    on = set(on or [])
    changed = []
    for key in dict.fromkeys(offered or []):
        addon, _, option_id = str(key).partition("/")
        try:
            if set_value(ws, ticket_id, addon, option_id, key in on, actor):
                changed.append(key)
        except UsageError:
            continue
    return changed
