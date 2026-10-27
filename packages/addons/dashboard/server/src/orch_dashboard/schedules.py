"""The "My schedules" tab (Schedules, #720): the workspace's schedules, read from the host for the page.

Read only. Adding and cancelling a schedule are signed hand-offs (`handoff.py`: `schedule_add`, `schedule_cancel`);
this route only says what exists, who may cancel it and when it runs next. The host decides every one of those again
when a request is prepared and signed.
"""

import time
import unicodedata
from datetime import UTC, datetime

from fastapi import Depends, FastAPI

from .errors import HostError
from .gateway import Gateway
from .reads import workspace

MAX_ROWS = 200
MAX_BRIEF = 4096
MANAGERS = ("owner", "maintainer")
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


def next_run(row: dict, now: int) -> str | None:
    """When a live schedule runs next, from now (core `occurrences`); null for one that is over or cannot be read."""
    if row.get("state") != "live":
        return None
    from orch.model.schedules import occurrences

    starts = _epoch(row.get("starts"))
    if starts is None:
        return None
    try:
        when = occurrences(row.get("rule") or {}, starts, _epoch(row.get("ends")), now)
    except (ValueError, KeyError, TypeError):
        return None
    return None if when is None else _iso(when)


def _title(gateway: Gateway, ws: str, key: str, seen: dict) -> str | None:
    if key not in seen:
        try:
            doc = gateway.call(ws, "ticket.get", {"ref": key}).get("doc") or {}
            seen[key] = doc.get("title") if isinstance(doc.get("title"), str) else None
        except HostError:
            seen[key] = None
    return seen[key]


def build(gateway: Gateway, ws: str, now: int | None = None) -> dict:
    """The page's data. A workspace without the Schedules add-on is `available: false` with nothing else."""
    link = gateway.enabled(ws)
    peer = link.welcome.get("peer") or {}
    me, role = peer.get("person"), peer.get("role")
    if not gateway.addon_on(link, "schedules"):
        return {"available": False, "role": role, "can": {"add": False, "cancel": False}, "rows": []}
    now = int(time.time()) if now is None else now
    listed = gateway.call(ws, "schedule.list", {}).get("rows") or []
    titles: dict = {}
    rows = []
    for row in listed[:MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        mine = row.get("person") == me
        rows.append(
            {
                "id": row.get("id"),
                "ticket": key,
                "title": _title(gateway, ws, key, titles) if isinstance(key, str) else None,
                "state": row.get("state"),
                "rule": row.get("rule") or {},
                "ends": row.get("ends"),
                "next_run": next_run(row, now),
                "brief": note(row.get("brief")),
                "mine": mine,
                "can_cancel": mine or role in MANAGERS,
                "label": row.get("label"),
            }
        )
    return {
        "available": True,
        "role": role,
        "can": {"add": role in WRITERS, "cancel": role in WRITERS},
        "rows": rows,
    }


def register(app: FastAPI, gateway: Gateway):
    @app.get("/api/w/{ws}/schedules")
    def schedules(ws: str = Depends(workspace)):
        return build(gateway, ws)
