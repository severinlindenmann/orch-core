"""Ticket links of the explorer (issue #491, decision 3 and 4): derived from host events ONLY.

A commit belongs to a ticket when its id is in that ticket's `branch.pushed` event (`ticket.get` `pushes[].sha`) or in a
`task.done` receipt (`tasks[].receipt.commit`). No commit message is parsed, nothing new is stored: the index below is a
memory cache of what the host said, rebuilt from `ticket.query` / `ticket.get` (the host decides which tickets this
person may see).
"""

from __future__ import annotations

import re
import threading
import time

from ..adapters import board_statuses
from ..adapters.addons import display
from ..errors import HostError

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$")
MAX_TICKETS = 400
#: per refresh, at most this many tickets are read from the host (the rest follow on the next request)
PER_REFRESH = 120
TICKS = 3.0  # seconds a refresh may take
FRESH = 20.0  # seconds before the list of tickets is asked again

_lock = threading.Lock()
#: workspace -> {"at": monotonic, "tickets": {uid: (updated_at, key, title, {sha: ...}, receipts)}}
_cache: dict[str, dict] = {}


def _read(link, key: str) -> tuple[set[str], list[dict]]:
    data = link.call("ticket.get", {"ref": key, "log_limit": 1})
    shas: set[str] = set()
    pushes = []
    # An open ticket's first sight of its branch (no "before") may be the commit the branch was cut from, which is not
    # work of this ticket: the readers check that against the repository (git.on_head) before they show it.
    still_open = data.get("status") not in (None, "done", "closed")
    for p in data.get("pushes") or []:
        if isinstance(p, dict) and isinstance(p.get("sha"), str) and SHA.fullmatch(p["sha"]):
            shas.add(p["sha"])
            before = p.get("before") if isinstance(p.get("before"), dict) else {}
            old = before.get("sha") if isinstance(before.get("sha"), str) and SHA.fullmatch(before["sha"]) else None
            repo = display(p.get("repo_name"))
            pushes.append(
                {"repo": repo, "sha": p["sha"], "before": old, "via": "push", "baseline": old is None and still_open}
            )
    for t in data.get("tasks") or []:
        r = t.get("receipt") if isinstance(t, dict) else None
        if isinstance(r, dict) and isinstance(r.get("commit"), str) and SHA.fullmatch(r["commit"]):
            shas.add(r["commit"])
            pushes.append(
                {"repo": display(r.get("repo")), "sha": r["commit"], "before": None, "via": "task", "baseline": False}
            )
    return shas, pushes


def refresh(link, ws: str) -> dict:
    """The ticket index of ``ws``: ``{uid: (updated_at, key, title, shas, pushes)}`` (bounded work per call)."""
    with _lock:
        state = _cache.setdefault(ws, {"at": 0.0, "tickets": {}})
        now = time.monotonic()
        if now - state["at"] < FRESH:
            return state["tickets"]
        rows, page = [], None
        statuses = [*board_statuses(), "closed"]
        for _ in range(MAX_TICKETS // 200):
            args = {"status": statuses, "sort": "updated", "limit": 200, **({"page": page} if page else {})}
            data = link.call("ticket.query", args)
            rows += [r for r in data.get("rows") or [] if isinstance(r, dict)]
            page = data.get("next_page")
            if not page:
                break
        known = state["tickets"]
        fresh: dict = {}
        started, reads = time.monotonic(), 0
        for row in rows[:MAX_TICKETS]:
            uid, key, stamp = row.get("uid"), row.get("key"), row.get("updated_at")
            if not isinstance(uid, str) or not isinstance(key, str) or not KEY.fullmatch(key):
                continue
            old = known.get(uid)
            if old and old[0] == stamp:
                fresh[uid] = old
                continue
            if reads >= PER_REFRESH or time.monotonic() - started > TICKS:
                if old:
                    fresh[uid] = old
                continue
            reads += 1
            try:
                shas, pushes = _read(link, key)
            except HostError:
                continue
            fresh[uid] = (stamp, key, display(row.get("title")), shas, pushes)
        state["tickets"] = fresh
        state["at"] = now if reads < PER_REFRESH else 0.0  # unfinished: ask again at the next request
        return fresh


def chips(link, ws: str, shas: list[str], *, is_base=None) -> dict[str, list[dict]]:
    """``sha -> [{key, title}]`` for the commits of one history page. ``is_base(sha)`` says a commit is one a ticket's
    branch was cut from (already on the repository's own line): that is no work of the ticket."""
    want = {s for s in shas if SHA.fullmatch(s)}
    out: dict[str, list[dict]] = {}
    if not want:
        return out
    for _uid, (_stamp, key, title, found, pushes) in refresh(link, ws).items():
        for sha in found & want:
            own = [p for p in pushes if p["sha"] == sha]
            if is_base is not None and not any(not p["baseline"] for p in own) and is_base(sha):
                continue
            out.setdefault(sha, []).append({"key": key, "title": title})
    return {s: sorted(rows, key=lambda r: r["key"]) for s, rows in out.items()}


def ticket_commits(link, key: str) -> list[dict]:
    """The commits a ticket links to, from ITS events: ``[{repo, sha, before, via}]`` (newest last)."""
    return _read(link, key)[1]
