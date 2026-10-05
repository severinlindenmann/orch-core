"""What the host's heartbeat tells the relay's status page (presence, R9): three counts and the AI Factory state,
derived from this workspace's own data on every beat. Counts and codes only: no names, titles or text, because the
heartbeat travels in the clear.

    sessions      the workspace's running terminal sessions (Terminals, orch's tmux server)
    in_progress   tickets whose status is in-progress
    needs_you     the needs-you count every other surface shows (query.counts(query.waiting())["blocking"])
    factory       none | running | paused | waiting | ready | stopped | done, of the factory epic that most needs the
                  human (stopped, then waiting, ready, running, paused, done); none while the factory is switched
                  off or no epic carries a signed factory charter
    children_done, children_total   that epic's children: done (backed by a signed verdict or close) and all
    budget_pct    the larger of the child budget and the time budget used, in percent

Anything that cannot be read is left out or counted as zero, never guessed upwards.
"""
from __future__ import annotations

COUNT_MAX = 9999
PCT_MAX = 999
_RANK = ("stopped", "waiting", "ready", "running", "paused", "done")


def _count(n) -> int:
    return max(0, min(COUNT_MAX, int(n)))


def factory(ws) -> dict:
    """{"factory": code, and for an epic: children_done, children_total, budget_pct when known}."""
    from orch.core import epics, factory_report, ledger, permits, store
    from orch.core.events import read_events
    if not permits.enabled(ws):
        return {"factory": "none"}
    entries = store.scan(ws)
    signed, events = ledger.entries(ws), read_events(ws)
    asked = {r["epic"] for r in permits.open_requests(ws, signed, events)}
    best = None
    for e in entries:
        if e.meta is None or not epics.is_epic(e.meta):
            continue
        try:
            epic = store.read_ticket(e.path)
            d = permits.factory_delegation(ws, epic, signed)
            if d is None:
                continue
            if epic.status == "done":
                state = "done"
            elif factory_report.stopped(ws, epic, entries=entries, signed=signed, events=events):
                state = "stopped"
            elif epic.id in asked:
                state = "waiting"
            elif factory_report.ready(ws, epic, entries=entries, signed=signed, events=events):
                state = "ready"
            elif d.get("active"):
                state = "running"
            else:
                state = "paused"
        except Exception:  # noqa: BLE001 - an epic that cannot be read says nothing
            continue
        if best is None or _RANK.index(state) < _RANK.index(best[0]):
            best = (state, epic, d)
    if best is None:
        return {"factory": "none"}
    state, epic, d = best
    out = {"factory": state}
    try:
        kids = [store.read_ticket(k.path) for k in epics.children(ws, epic.id, entries)]
        done = sum(1 for k in kids if factory_report.finished(ws, k, signed, events) == "done")
        out.update(children_done=_count(done), children_total=_count(len(kids)))
    except Exception:  # noqa: BLE001
        pass
    pct = _budget_pct(ws, epic, d, events, entries)
    if pct is not None:
        out["budget_pct"] = pct
    return out


def _budget_pct(ws, epic, d, events, entries) -> int | None:
    from datetime import timedelta
    from orch import clock
    from orch.core import epics
    try:
        used = []
        if d.get("max_children"):
            used.append(epics.delegated_count(ws, epic.id, d["id"], events, entries) / d["max_children"])
        if d.get("max_hours") and d.get("at"):
            elapsed = clock.now() - clock.parse_stamp(str(d["at"]))
            used.append(elapsed / timedelta(hours=d["max_hours"]))
        return max(0, min(PCT_MAX, int(max(used) * 100))) if used else None
    except Exception:  # noqa: BLE001
        return None


def heartbeat(ws) -> dict:
    """The heartbeat's fields, read now."""
    from orch.core import query, store
    from orch.dashboard import terminals
    out = {}
    try:
        out["sessions"] = _count(len(terminals.sessions(ws)))
    except Exception:  # noqa: BLE001
        out["sessions"] = 0
    try:
        out["in_progress"] = _count(sum(1 for e in store.scan(ws) if e.status == "in-progress"))
    except Exception:  # noqa: BLE001
        out["in_progress"] = 0
    try:
        out["needs_you"] = _count(query.counts(query.waiting(ws))["blocking"])
    except Exception:  # noqa: BLE001
        out["needs_you"] = 0
    try:
        out.update(factory(ws))
    except Exception:  # noqa: BLE001
        out["factory"] = "none"
    return out
