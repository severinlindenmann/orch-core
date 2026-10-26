"""The owner's review after a leave (design-c8 M5): what the left hub did that the owner may want to undo.

Pure over the log lines (no I/O, no clock). The order is the point: **first** the hub-made lines that keep acting
without the hub (``policy.changed``, ``settings.changed``, ``device.excluded``, ``grant.requested``), each with a local
revert the owner can run standalone; **then** the tickets left with a gate nobody can open without the hub; **then**
every other admitted ``auth: "hub"`` line of that hub since the point the owner names (default 7 days). Text taken from
the log is data: it is cut and stripped of control characters before it is shown.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from orch.canon.text import clean_line
from orch.model.types import DEFAULT_SETTINGS, default_policies, ts

__all__ = ["DEFAULT_SINCE_S", "FIRST_TYPES", "build"]

DEFAULT_SINCE_S = 7 * 24 * 3600
#: the types whose effect outlives the hub, listed first and always (any age)
FIRST_TYPES = ("policy.changed", "settings.changed", "device.excluded", "grant.requested")
_MAX_OTHER = 500


def _who(e: Mapping[str, Any]) -> dict[str, Any]:
    a = e.get("actor", {})
    return {
        "user": clean_line(str(a.get("id", ""))),
        "channel": clean_line(str(e.get("channel", ""))),
        "factor": clean_line(str(e.get("channel_factor", ""))),
        "at": e.get("at"),
    }


def _by_hub(e: Mapping[str, Any], hub_id: str) -> bool:
    return e.get("auth") == "hub" and e.get("hub_id") == hub_id


def build(
    events: Iterable[tuple[str, Mapping[str, Any]]],
    *,
    hub_id: str,
    invalid: frozenset[str] = frozenset(),
    stuck: Iterable[Mapping[str, Any]] = (),
    since: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """``events``: ``(log, event)`` in merged order, every log. ``invalid``: ids of lines the model refused (never
    listed). ``stuck``: the gates the host found unsatisfiable (see ``host.breakglass``). ``since``: a timestamp; the
    third list starts there (default: ``now`` minus 7 days; with neither, everything)."""
    floor = since if since is not None else (None if now is None else _minus(now, DEFAULT_SINCE_S))
    policies = default_policies()
    settings = dict(DEFAULT_SETTINGS)
    overrides: dict[str, dict[str, Any]] = {}  # ticket log -> gate -> policy
    first: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for log, e in events:
        typ = e.get("type")
        if e.get("id") in invalid:
            continue
        mine = _by_hub(e, hub_id)
        item: dict[str, Any] | None = None
        if typ == "policy.changed":
            gates = e.get("gates", {})
            held = overrides.setdefault(log, {}) if log != "workspace" else policies
            prev = {g: copy.deepcopy(held.get(g)) for g in gates}
            if mine:
                item = {"gates": sorted(gates), "scope": "workspace" if log == "workspace" else "ticket",
                        "revert": {"op": "policy.set", "log": log, "gates": prev}}  # fmt: skip
            for g, p in gates.items():
                held[g] = copy.deepcopy(p)
        elif typ == "settings.changed":
            changes = e.get("set", {})
            prev_s = {k: settings.get(k) for k in changes}
            if mine:
                item = {"keys": sorted(changes), "revert": {"op": "settings.set", "set": prev_s}}
            for k, v in changes.items():
                if v is None:
                    settings.pop(k, None)
                else:
                    settings[k] = v
        elif typ == "device.excluded" and mine:
            item = {"device": clean_line(str(e.get("device", ""))), "reason": clean_line(str(e.get("reason", ""))),
                    "revert": None, "note": "no inverse in V3: the device stays unusable until re-added"}  # fmt: skip
        elif typ == "grant.requested" and mine:
            item = {"scope": e.get("scope"), "hours": e.get("hours"), "revert": None,
                    "note": "ended at the leave; the grant and its secret are gone"}  # fmt: skip
        if not mine:
            continue
        base = {"type": typ, "id": e.get("id"), "log": log, **_who(e)}
        if typ in FIRST_TYPES and item is not None:
            first.append({**base, **item})
        elif floor is None or (e.get("at") or "") >= floor:
            other.append({**base, "ticket": None if log == "workspace" else log})
    return {
        "hub_id": hub_id,
        "first": first,
        "stuck": [dict(s) for s in stuck],
        "other": other[-_MAX_OTHER:],
        "other_cut": max(0, len(other) - _MAX_OTHER),
        "since": floor,
    }


def _minus(now: str, seconds: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts(now) - seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
