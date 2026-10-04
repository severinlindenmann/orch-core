"""Which local ticket an outside item belongs to (spec v2 §11.3, §11.4: linked tickets and out-of-sync are computed
by the core, never by an addon). Addons get a LinkIndex through ctx.links() / view.links(); it only reads tickets."""
from __future__ import annotations

import re
from dataclasses import dataclass

from orch.core import store, trackers
from orch.core.ids import normalize_ref

MAX_TEXT = 300  # outside text (titles, branches) that tracker patterns run against


@dataclass(frozen=True)
class LinkedTicket:
    id: str
    title: str
    status: str
    external: tuple = ()
    prs: tuple = ()
    branches: tuple = ()
    blocked_by: tuple = ()


def sync_action(category, status) -> str | None:
    """'close' when the tracker is done and the local ticket is not; 'reopen' when the tracker is open again and the
    local ticket is done (spec v2 §13.2); else None."""
    if category == "done" and status != "done":
        return "close"
    if category in ("todo", "in_progress") and status == "done":
        return "reopen"
    return None


def _strings(value) -> tuple:
    return tuple(str(v) for v in value if isinstance(v, (str, int)) and str(v).strip()) if isinstance(value, list) else ()


class LinkIndex:
    def __init__(self, ws, entries=None):
        self._ws = ws
        self.tickets: list[LinkedTicket] = []
        self._by_id: dict[str, LinkedTicket] = {}
        self._by_external: dict[str, list[LinkedTicket]] = {}
        self._by_pr: dict[str, LinkedTicket] = {}
        self._by_branch: dict[str, LinkedTicket] = {}
        for e in store.scan(ws) if entries is None else entries:
            meta = e.meta if isinstance(e.meta, dict) else None
            if meta is None:
                continue
            ext = tuple(str(x["key"]).strip().upper() for x in meta.get("external") or []
                        if isinstance(x, dict) and isinstance(x.get("key"), (str, int)) and str(x["key"]).strip())
            prs = tuple(p["url"] for p in meta.get("prs") or [] if isinstance(p, dict) and isinstance(p.get("url"), str))
            branches = meta.get("branches") if isinstance(meta.get("branches"), dict) else {}
            t = LinkedTicket(e.id, str(meta.get("title") or ""), str(meta.get("status") or e.status), ext, prs,
                             tuple(b for b in branches.values() if isinstance(b, str) and b), _strings(meta.get("blocked_by")))
            self.tickets.append(t)
            self._by_id[e.id.upper()] = t
            for key in ext:
                # a key may be linked from more than one ticket: keep every one of them, not just the first
                self._by_external.setdefault(key, []).append(t)
            for url in prs:
                self._by_pr.setdefault(url, t)
            for branch in t.branches:
                self._by_branch.setdefault(branch, t)
        prefix = ws.config["id"]["prefix"]
        self._local = re.compile(rf"(?<![A-Za-z0-9]){re.escape(prefix)}-\d+(?![0-9])", re.IGNORECASE)
        self._external: list[re.Pattern] = []
        for tracker in ws.config.get("external_trackers") or []:
            pattern = tracker.get("pattern") if isinstance(tracker, dict) else None
            if not isinstance(pattern, str) or trackers.accepts_bare_number(pattern):
                continue  # \d+ would link any number in a title; orch check reports it, orch migrate fixes it
            try:
                self._external.append(re.compile(rf"(?<![A-Za-z0-9])(?:{trackers.plain(pattern)})(?![A-Za-z0-9])", re.IGNORECASE))
            except re.error:
                continue

    def _local_id(self, ref: str) -> str:
        return normalize_ref(self._ws, ref.strip().upper()).upper()

    def ticket(self, ref) -> LinkedTicket | None:
        ref = str(ref).strip()
        found = self._by_id.get(self._local_id(ref))
        if found is not None:
            return found
        matches = self._by_external.get(ref.upper())
        return matches[0] if matches else None

    def for_external(self, key) -> list[LinkedTicket]:
        """Every local ticket linked to this external key, in link order (more than one is possible)."""
        return list(self._by_external.get(str(key).strip().upper(), ()))

    def keys_in(self, *texts) -> list[str]:
        """Ticket ids and external keys named in outside text (a PR title, a branch), each cut to MAX_TEXT characters
        so a tracker pattern never runs over unbounded input."""
        out: list[str] = []
        for text in texts:
            if not isinstance(text, str):
                continue
            text = text[:MAX_TEXT]
            found = [self._local_id(m.group(0)) for m in self._local.finditer(text)]
            found += [m.group(0).upper() for rx in self._external for m in rx.finditer(text)]
            out.extend(k for k in found if k not in out)
        return out

    def for_review(self, *, url: str = "", branch: str = "", title: str = "", body: str = "") -> list[LinkedTicket]:
        """The tickets a review (PR/MR) belongs to: linked by URL or branch, or named in its branch, title or body
        (#14; each text is cut to MAX_TEXT, so pass the body's key-like words, not a whole description)."""
        out: list[LinkedTicket] = []

        def add(t):
            if t is not None and t not in out:
                out.append(t)

        add(self._by_pr.get(url))
        add(self._by_branch.get(branch))
        for key in self.keys_in(branch, title, body):
            local = self._by_id.get(key)
            if local is not None:
                add(local)
            for t in self._by_external.get(key, ()):
                add(t)
        return out

    def sync(self, key, category) -> list[tuple[LinkedTicket, str]]:
        """(ticket, "close" | "reopen") for every ticket linked to `key` that disagrees with `category`; evaluated
        per ticket, since one external key can be linked from more than one local ticket."""
        out = []
        for t in self.for_external(key):
            action = sync_action(category, t.status)
            if action:
                out.append((t, action))
        return out

    def merge_order(self, ids) -> list[str] | None:
        """`ids` ordered so that blockers come first (by `blocked_by` among these tickets); None when no such link
        exists or the links form a cycle."""
        wanted: list[str] = []
        for ref in ids:
            t = self.ticket(ref)
            if t is not None and t.id not in wanted:
                wanted.append(t.id)
        deps = {tid: [b.id for ref in self._by_id[tid.upper()].blocked_by
                      if (b := self.ticket(ref)) is not None and b.id in wanted and b.id != tid] for tid in wanted}
        if not any(deps.values()):
            return None
        order: list[str] = []
        state: dict[str, str] = {}

        def visit(tid: str) -> bool:
            if state.get(tid) == "done":
                return True
            if state.get(tid) == "busy":
                return False
            state[tid] = "busy"
            if not all(visit(d) for d in deps[tid]):
                return False
            state[tid] = "done"
            order.append(tid)
            return True

        return order if all(visit(tid) for tid in wanted) else None


def shared_index(ws) -> LinkIndex:
    """One LinkIndex per request (store.request_scope), shared by every addon widget and the core; a fresh one
    outside a scope. Read-only, so one instance serves every reader."""
    return store.memo(ws, "link-index", lambda: LinkIndex(ws))
