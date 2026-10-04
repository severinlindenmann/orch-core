"""`orch wait`: an agent blocks until the human acts on its ticket. Read-only: it takes no
lock, writes no event and needs no human. It reads events.jsonl again only when the file changed size, and
sleeps a bounded interval between looks."""
from __future__ import annotations

import time

from orch.core import store
from orch.core.events import events_path, last_seq, read_events

HUMAN_EVENT_KINDS = ("question.answered", "gate.approved", "gate.changes_requested", "verdict.given",
                     "permit.granted", "permit.denied")
MIN_POLL, MAX_POLL = 0.01, 5.0  # seconds; never a busy loop, never a long nap


def _is_agents_own(event) -> bool:
    """An event an agent session wrote itself: not the human's, not an addon's, not `orch check`'s."""
    via = str(event.via or "")
    return str(event.actor).startswith("agent:") and via != "check" and not via.startswith("addon:")


def default_cursor(ws, ticket_id: str) -> int:
    """The agent's own last event on the ticket, so a decision made between `orch ask` and `orch wait`
    still counts. Without any agent event on it: the newest event (only new decisions count)."""
    mine = [e.seq for e in read_events(ws, ticket_id) if _is_agents_own(e)]
    return max(mine) if mine else last_seq(ws)


def _match(event, ticket_id: str) -> bool:
    return event.ticket == ticket_id and event.kind in HUMAN_EVENT_KINDS and str(event.actor).startswith("human:")


def wait_for_human(ws, ref: str, *, after: int | None = None, timeout: float = 0.0, poll: float = 1.0,
                   clock=time.monotonic, sleep=time.sleep):
    """The first human decision event on `ref` after `after`, or None once `timeout` seconds passed (0 = no limit)."""
    ticket_id = store.resolve(ws, ref).id
    cursor = default_cursor(ws, ticket_id) if after is None else int(after)
    interval = min(max(float(poll or 0), MIN_POLL), MAX_POLL)
    deadline = clock() + timeout if timeout and timeout > 0 else None
    path, size = events_path(ws), -1
    while True:
        try:
            current = path.stat().st_size
        except FileNotFoundError:
            current = 0
        if current != size:
            size = current
            for event in read_events(ws, ticket_id, after=cursor):
                if _match(event, ticket_id):
                    return event
        if deadline is None:
            sleep(interval)
            continue
        left = deadline - clock()
        if left <= 0:
            return None
        sleep(min(interval, left))
