"""Bounded host ticket index, isolated by workspace AND signed-in person."""

import threading
import time

from ..adapters import board_statuses
from ..adapters.addons import display
from ..errors import HostError
from ..explorer.links import KEY
from . import limits
from .reader import SLUG

_lock = threading.Lock()
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


def links_by_slug(link, ws: str, person: str) -> dict[str, list[dict]]:
    with _lock:
        state = _cache.setdefault((ws, person), {"at": None, "tickets": {}})
        now = time.monotonic()
        if state["at"] is None or now - state["at"] >= limits.FRESH:
            rows, page = [], None
            for _ in range((limits.MAX_TICKETS + 199) // 200):
                args = {"status": [*board_statuses(), "closed"], "sort": "updated", "limit": 200}
                if page:
                    args["page"] = page
                data = link.call("ticket.query", args)
                rows.extend(r for r in data.get("rows", []) if isinstance(r, dict))
                page = data.get("next_page")
                if not page:
                    break
            fresh = {}
            started, reads, unfinished = time.monotonic(), 0, False
            for row in rows[: limits.MAX_TICKETS]:
                uid, key, stamp = row.get("uid"), row.get("key"), row.get("updated_at")
                if not isinstance(uid, str) or not isinstance(key, str) or not KEY.fullmatch(key):
                    continue
                old = state["tickets"].get(uid)
                title = display(row.get("title")) or ""
                if old and old[0] == stamp and old[1] == key:
                    fresh[uid] = (stamp, key, title, old[3])
                    continue
                if reads >= limits.PER_REFRESH or time.monotonic() - started >= limits.REFRESH_SECONDS:
                    unfinished = True
                    continue
                reads += 1
                try:
                    slugs = _read(link, key)
                except HostError:
                    unfinished = True
                    continue
                fresh[uid] = (stamp, key, title, slugs)
            state.update(tickets=fresh, at=None if unfinished else now)
        out = {}
        for _stamp, key, title, slugs in state["tickets"].values():
            for slug in slugs:
                out.setdefault(slug, []).append({"ticket": key, "title": title})
        return {slug: sorted(rows, key=lambda r: r["ticket"]) for slug, rows in out.items()}
