"""Bounded host ticket index, isolated by workspace AND signed-in person."""

import threading
import time

from ..adapters import board_statuses
from ..adapters.addons import display
from ..errors import HostError
from ..explorer.links import KEY
from . import limits
from .reader import SLUG

_guard = threading.Lock()
_locks: dict[tuple[str, str], threading.Lock] = {}
_cache: dict[tuple[str, str], dict] = {}


def parse_pages(value) -> list[str]:
    if not isinstance(value, str):
        return []
    out = []
    for token in value.split():
        if SLUG.fullmatch(token) and token not in out:
            out.append(token)
            if len(out) == limits.LINK_SLUGS:
                break
    return out


def _read(link, key):
    data = link.call("ticket.get", {"ref": key, "log_limit": 1})
    for part in ("addons", "wiki", "pages"):
        data = data.get(part) if isinstance(data, dict) else None
    return parse_pages(data)


def _lock_of(key) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def links_by_slug(link, ws: str, person: str) -> dict[str, list[dict]]:
    """``{slug: [{"ticket", "title"}]}`` for the tickets THIS person can see. The host's ticket list is asked on EVERY
    call (it is the visibility decision, never cached); only the expensive per-ticket reads are remembered, by the
    ticket's own change stamp, so a ticket that left the person's list leaves the answer at once. One deadline
    (REFRESH_SECONDS) covers the list and the reads; what is not read yet follows on the next call."""
    key = (ws, person)
    with _lock_of(key):  # one refresh per (workspace, person) at a time; nobody else waits for it
        known = _cache.setdefault(key, {})
        started = time.monotonic()
        rows, page = [], None
        try:
            for _ in range((limits.MAX_TICKETS + 199) // 200):
                args = {"status": [*board_statuses(), "closed"], "sort": "updated", "limit": 200}
                if page:
                    args["page"] = page
                data = link.call("ticket.query", args)
                rows.extend(r for r in data.get("rows", []) if isinstance(r, dict))
                page = data.get("next_page")
                if not page or time.monotonic() - started >= limits.REFRESH_SECONDS:
                    break
        except (HostError, AttributeError, TypeError):
            return {}  # links are a courtesy: pages stay readable when the host cannot list tickets (nothing cached)
        fresh, reads = {}, 0
        for row in rows[: limits.MAX_TICKETS]:
            uid, tkey, stamp = row.get("uid"), row.get("key"), row.get("updated_at")
            if not isinstance(uid, str) or not isinstance(tkey, str) or not KEY.fullmatch(tkey):
                continue
            title = display(row.get("title")) or ""
            old = known.get(uid)
            if old and old[0] == stamp and old[1] == tkey:
                fresh[uid] = (stamp, tkey, title, old[3])
                continue
            if reads >= limits.PER_REFRESH or time.monotonic() - started >= limits.REFRESH_SECONDS:
                continue
            reads += 1
            try:
                fresh[uid] = (stamp, tkey, title, _read(link, tkey))
            except HostError:
                continue
        _cache[key] = fresh  # only tickets in THIS answer of the host stay
        out: dict[str, list[dict]] = {}
        for _stamp, tkey, title, slugs in fresh.values():
            for slug in slugs:
                out.setdefault(slug, []).append({"ticket": tkey, "title": title})
        return {slug: sorted(rows, key=lambda r: r["ticket"]) for slug, rows in out.items()}
