"""The "My schedules" tab (Schedules, #720): the workspace's schedules, read from the host for the page.

Read only. Adding and cancelling a schedule are signed hand-offs (`handoff.py`: `schedule_add`, `schedule_cancel`);
this route only says what exists, who may cancel it and when it runs next. The host decides every one of those again
when a request is prepared and signed.
"""

import time
import unicodedata
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import Depends, FastAPI

from .errors import HostError
from .gateway import Gateway
from .reads import workspace

MAX_ROWS = 200
MAX_BRIEF = 4096
WRITERS = ("owner", "maintainer", "member")


def note(text) -> str:
    """A brief as plain text: no control, bidi or zero-width characters (newlines and tabs stay), at most 4096."""
    if not isinstance(text, str):
        return ""
    kept = "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))
    return kept[:MAX_BRIEF]


def _epoch(value) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return int(when.timestamp())
    return None


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recorded(row: dict) -> list[dict]:
    """The settled occurrences, oldest first: core's `outcomes` list, else the `occurrences` map's settled ones."""
    listed = row.get("outcomes")
    if isinstance(listed, list):
        return [o for o in listed if isinstance(o, dict) and isinstance(o.get("occurrence"), str)]
    held = row.get("occurrences")
    if not isinstance(held, dict):
        return []
    return [
        {"occurrence": at, "outcome": o.get("outcome"), "reason": o.get("reason")}
        for at, o in sorted(held.items())
        if isinstance(o, dict) and o.get("outcome")
    ]


def next_run(row: dict, now: int, prefix: str = "") -> str | None:
    """When a live schedule runs next: the first occurrence of the rule after now, after the latest recorded one
    (fired or settled, never offered again) and not before the model's `first_allowed` (the later of `starts` and
    `set_at` plus the minimum lead; a dev sandbox workspace has a shorter lead). Null for one that is over or cannot
    be read."""
    if row.get("state") != "live":
        return None
    from orch.model.schedules import occurrences
    from orch.model.schedules_wire import first_allowed

    starts = _epoch(row.get("starts"))
    if starts is None:
        return None
    held = row.get("occurrences")
    try:
        # A host that does not say `set_at` leaves only `starts` as the floor.
        rec = SimpleNamespace(starts=row["starts"], set_at=row.get("set_at") or "1970-01-01T00:00:00Z")
        after = max(
            first_allowed(SimpleNamespace(prefix=prefix, settings={}), rec) - 1,
            now,
            *(_epoch(o) or 0 for o in (held if isinstance(held, dict) else ())),
            *(_epoch(o["occurrence"]) or 0 for o in _recorded(row)),
        )
        when = occurrences(row.get("rule") or {}, starts, _epoch(row.get("ends")), after)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return None if when is None else _iso(when)


def last_outcome(row: dict) -> dict | None:
    """The latest settled occurrence as `{at, outcome, reason}`; the reason only from the model's closed list."""
    from orch.model.followups import OUTCOMES
    from orch.model.schedules import REASONS

    for o in reversed(_recorded(row)):
        if o.get("outcome") in OUTCOMES:
            reason = o.get("reason")
            return {"at": o["occurrence"], "outcome": o["outcome"], "reason": reason if reason in REASONS else None}
    return None


def _int(value) -> int | None:
    return value if type(value) is int else None


def max_run_hours(row: dict) -> int | None:
    """The longest a firing's run lasts: `run_hours`, capped by the setter's `grant_hours_cap` when the host says it."""
    hours, cap = _int(row.get("run_hours")), _int(row.get("grant_hours_cap"))
    return hours if hours is None or cap is None else min(hours, cap)


def _title(gateway: Gateway, ws: str, key: str, seen: dict) -> str | None:
    if key not in seen:
        try:
            doc = gateway.call(ws, "ticket.get", {"ref": key}).get("doc") or {}
            seen[key] = doc.get("title") if isinstance(doc.get("title"), str) else None
        except HostError:
            seen[key] = None
    return seen[key]


def _prefix(gateway: Gateway, ws: str) -> str:
    """The workspace's prefix (the model gives a dev sandbox workspace its shorter minimum lead); empty if unread."""
    try:
        prefix = gateway.call(ws, "workspace.show", {}).get("prefix")
    except HostError:
        return ""
    return prefix if isinstance(prefix, str) else ""


def min_lead_s(prefix: str) -> int:
    """The minimum scheduling lead the host enforces (`schedule.denied` below it): core's own caps of a workspace with
    this prefix (a dev sandbox workspace has a shorter one). The form offers no earlier time."""
    from orch.model.engine import DEV_PREFIX
    from orch.model.followups import effective_caps

    return effective_caps({}, dev=prefix == DEV_PREFIX).min_lead_s


def build(gateway: Gateway, ws: str, now: int | None = None) -> dict:
    """The page's data. A workspace without the Schedules add-on is `available: false` with nothing else."""
    link = gateway.enabled(ws)
    peer = link.welcome.get("peer") or {}
    me, role = peer.get("person"), peer.get("role")
    if not gateway.addon_on(link, "schedules"):
        return {
            "available": False,
            "role": role,
            "can": {"add": False, "cancel": False},
            "grant_hours_cap": None,
            "min_lead_s": None,
            "rows": [],
        }
    now = int(time.time()) if now is None else now
    listed = gateway.call(ws, "schedule.list", {}).get("rows") or []
    titles: dict = {}
    rows = []
    cap = None  # the viewer's own grant_hours cap, as the host says it on their schedules
    prefix = _prefix(gateway, ws)  # also the minimum lead of a new schedule
    for row in listed[:MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        if row.get("target", "existing") != "existing":
            # one row this dashboard cannot read must not break the page: it is listed as unsupported
            rows.append(
                {
                    "id": row.get("id"),
                    "ticket": None,
                    "title": None,
                    "state": row.get("state"),
                    "rule": {},
                    "ends": None,
                    "brief": "",
                    "mine": row.get("person") == me,
                    "can_cancel": False,
                    "supported": False,
                    "note": "Needs a newer dashboard",
                }
            )
            continue
        key = row.get("key")
        mine = row.get("person") == me
        if mine and cap is None:
            cap = _int(row.get("grant_hours_cap"))
        rows.append(
            {
                "id": row.get("id"),
                "ticket": key,
                "title": _title(gateway, ws, key, titles) if isinstance(key, str) else None,
                "state": row.get("state"),
                "rule": row.get("rule") or {},
                "ends": row.get("ends"),
                "next_run": next_run(row, now, prefix),
                "last_outcome": last_outcome(row),
                "brief": note(row.get("brief")),
                "mine": mine,
                # the model's rule (schedules_wire.cancelled_event): the setter or an owner
                "can_cancel": mine or role == "owner",
                "label": note(row.get("label")) or None,
                "run_hours": _int(row.get("run_hours")),
                "max_run_hours": max_run_hours(row),
            }
        )
    return {
        "available": True,
        "role": role,
        "can": {"add": role in WRITERS, "cancel": role in WRITERS},
        "grant_hours_cap": cap,
        "min_lead_s": min_lead_s(prefix),
        "rows": rows,
    }


def register(app: FastAPI, gateway: Gateway):
    @app.get("/api/w/{ws}/schedules")
    def schedules(ws: str = Depends(workspace)):
        return build(gateway, ws)
