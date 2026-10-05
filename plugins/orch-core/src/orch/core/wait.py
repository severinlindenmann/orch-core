"""`orch wait`: an agent blocks until the human acts on its ticket (or, in an AI Factory epic, until the factory is
Ready or Stopped). Read-only: it takes no
lock, writes no event and needs no human. It reads events.jsonl again only when the file changed size, and
sleeps a bounded interval between looks."""
from __future__ import annotations

import time

from orch.core import store
from orch.clock import stamp_s
from orch.core.events import Event, events_path, last_seq, read_events

HUMAN_EVENT_KINDS = ("question.answered", "gate.approved", "gate.changes_requested", "verdict.given",
                     "permit.granted", "permit.denied")
FACTORY_EVERY = 5.0  # seconds between looks at the factory's state
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


def _factory_epic(ws, ticket_id: str):
    """The ticket itself, when it is an AI Factory epic by its signed charter (a child's agent does not wait on the
    epic's state: that is the human's and the epic's own waiter's); else None."""
    try:
        from orch.core import epics, permits
        t = store.read_ticket(store.resolve(ws, ticket_id).path)
        return t if permits.enabled(ws) and epics.is_epic(t) and permits.charter_epic(ws, t) is not None else None
    except Exception:
        return None


def _parse_cursor(after) -> tuple[int, tuple[str, str] | None]:
    """`after` is an event seq, or the cursor a factory wake printed: "<seq>:<kind>:<token>", which also names the
    factory state already seen, so the same state does not wake the waiter again."""
    if isinstance(after, int):
        return after, None
    seq, _, seen = str(after).strip().partition(":")
    kind, _, token = seen.partition(":")
    return int(seq), ((kind, token) if kind and token else None)


def _match(event, ticket_id: str) -> bool:
    return event.ticket == ticket_id and event.kind in HUMAN_EVENT_KINDS and str(event.actor).startswith("human:")


def wait_for_human(ws, ref: str, *, after: int | str | None = None, timeout: float = 0.0, poll: float = 1.0,
                   clock=time.monotonic, sleep=time.sleep):
    """The first human decision event on `ref` after `after`, or None once `timeout` seconds passed (0 = no limit).
    On a factory epic, also a derived `factory.ready` / `factory.stopped` event, once per state: its cursor names the
    state, and passing that cursor back as `after` waits for the next change. The state is looked at when the event
    log changed and at most every FACTORY_EVERY seconds (it is time-dependent: the budget runs out)."""
    ticket_id = store.resolve(ws, ref).id
    cursor, seen = (default_cursor(ws, ticket_id), None) if after is None else _parse_cursor(after)
    interval = min(max(float(poll or 0), MIN_POLL), MAX_POLL)
    deadline = clock() + timeout if timeout and timeout > 0 else None
    path, size = events_path(ws), -1
    factory, next_look = _factory_epic(ws, ticket_id), 0.0
    while True:
        try:
            current = path.stat().st_size
        except FileNotFoundError:
            current = 0
        changed = current != size
        if changed:
            size = current
            for event in read_events(ws, ticket_id, after=cursor):
                if _match(event, ticket_id):
                    return event
        if factory is not None and (changed or clock() >= next_look):
            next_look = clock() + FACTORY_EVERY
            from orch.core import factory_report
            sig = factory_report.signal(ws, factory)
            if sig is not None and sig != seen:
                seq = last_seq(ws)
                return Event(seq, stamp_s(), factory.id, f"factory.{sig[0]}", "orch:factory", "derived",
                             {"cursor": f"{seq}:{sig[0]}:{sig[1]}"})
        if deadline is None:
            sleep(interval)
            continue
        left = deadline - clock()
        if left <= 0:
            return None
        sleep(min(interval, left))
