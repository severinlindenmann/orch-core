"""How long earlier factory runs took, for the run view's time line (docs/factory.md, "Estimates").

The runner (a human process) writes one record per finished run, once, beside the ledger in the permits folder
(`durations/`, guarded like the rest of it): the whole run, the planner's time, each child's time with its size, and
each release stage's time, all measured from orch's own event log and the signed charter's start. The run view shows
"about N minutes left (from M earlier runs)" only when at least MIN_RUNS comparable runs (the same release target)
are recorded and the median of their whole runs is still ahead; otherwise it says nothing new. A number is never made
up: no records, too few, or a run already past the median show no estimate."""
from __future__ import annotations

import math
import os
import statistics

from orch import clock
from orch.core import factory_sessions as fs

MIN_RUNS = 3
_KEYS = {"workspace", "delegation", "release", "total_s", "planner_s", "children", "stages", "at"}


def _dir():
    return fs._root() / "durations"


def _at(stamp):
    try:
        return clock.parse_stamp(str(stamp))
    except (ValueError, TypeError):
        return None


def _secs(a, b) -> int | None:
    return int((b - a).total_seconds()) if a and b and b >= a else None


def measure(ws, epic, d: dict, events, kids) -> dict | None:
    """The durations of a finished run, from events only; None when the run is not finished or its start unknown.
    `kids`: [(entry, Ticket)] of the epic's children."""
    from orch.core.factory_release import target_stages
    from orch.core.ledger import workspace_id
    eid = epic.id.upper()
    start = _at(d.get("at"))
    mine = [e for e in events if str(e.ticket).upper() == eid]
    end = max((_at(e.at) for e in mine if _at(e.at)), default=None)
    if epic.status != "done" or start is None or end is None:
        return None
    ids = {t.id.upper() for _, t in kids or []}
    created = [_at(e.at) for e in events if e.kind == "ticket.created" and str(e.ticket).upper() in ids and _at(e.at)]
    children, tested = [], []
    for _, t in kids or []:
        own = [e for e in events if str(e.ticket).upper() == t.id.upper()]
        claim = next((_at(e.at) for e in own if e.kind == "claim.taken"), None)
        moved = next((_at(e.at) for e in reversed(own) if e.kind == "ticket.moved"
                      and (e.data or {}).get("to") == "testing"), None)
        s = _secs(claim, moved)
        if s is not None:
            children.append([str(t.meta.get("size") or "?"), s])
            tested.append(moved)
    stages, prev = {}, max(tested, default=None)
    for name in target_stages(d.get("release")):
        done = [_at(e.at) for e in mine if e.kind == "release.stage" and (e.data or {}).get("stage") == name
                and (e.data or {}).get("proven") is True and _at(e.at)]
        if not done:
            break
        stages[name] = _secs(prev, max(done))
        prev = max(done)
    return {"workspace": workspace_id(ws), "delegation": str(d["id"]), "release": str(d.get("release") or "none"),
            "total_s": _secs(start, end), "planner_s": _secs(start, min(created, default=None)),
            "children": children, "stages": stages, "at": clock.stamp_s()}


def record(ws, actor, epic, d: dict, events, kids) -> bool:
    """Human only (the runner): write the record of a finished run, once. True when it was written now."""
    fs.human_check(actor, "recording a factory run's durations")
    body = measure(ws, epic, d, events, kids)
    if body is None or body["total_s"] is None:
        return False
    return fs._create(_dir() / f"{fs._key(ws, str(d['id']))}.json", body)


def records(ws) -> list[dict]:
    """This workspace's records that read back whole; anything else is left out (never guessed at)."""
    from orch.core.ledger import workspace_id
    wid, out = workspace_id(ws), []
    try:
        names = sorted(os.listdir(_dir()))
    except OSError:
        return []
    for n in names:
        body = fs._read_json(_dir() / n)
        if (body and set(body) == _KEYS and body["workspace"] == wid and isinstance(body["total_s"], int)
                and body["total_s"] >= 0):
            out.append(body)
    return out


def estimate(ws, d: dict, elapsed_s: float) -> dict | None:
    """{minutes, runs} left for a running run of delegation `d`, from at least MIN_RUNS comparable finished runs (the
    same release target), or None."""
    target = str(d.get("release") or "none")
    same = [r["total_s"] for r in records(ws) if r["release"] == target and r["delegation"] != str(d.get("id"))]
    if len(same) < MIN_RUNS:
        return None
    left = statistics.median(same) - max(0.0, float(elapsed_s))
    if left <= 0:
        return None  # longer than the earlier runs already: no number to give
    return {"minutes": max(1, math.ceil(left / 60)), "runs": len(same)}


def tick(ws, actor) -> list[str]:
    """One round: record every finished run of an armed factory epic that has no record yet."""
    from orch.core import epics, permits, store
    from orch.core.events import read_events
    from orch.core.factory_report import _kids
    out, events = [], None
    for entry in store.scan(ws):
        if entry.meta is None or not epics.is_epic(entry.meta) or entry.status != "done":
            continue
        try:
            epic = store.read_ticket(entry.path)
            d = permits.factory_delegation(ws, epic)
            if d is None or not fs.armed(ws, d["id"]) or (_dir() / f"{fs._key(ws, str(d['id']))}.json").exists():
                continue
            events = read_events(ws) if events is None else events
            if record(ws, actor, epic, d, events, _kids(ws, epic, None)):
                out.append(f"{epic.id}: run durations recorded")
        except Exception:
            continue  # an unreadable epic records nothing; the estimate then has one run fewer
    return out
