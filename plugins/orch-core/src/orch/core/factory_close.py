"""Dark AI Factory: the opt-in auto-close (docs/factory.md, "Closing by itself").

A Dark charter the human signed with `close: true` lets the runner (the dashboard process the human started, never an
agent) give the epic's done verdict by itself once everything is proven. That replaces the human verdict for that run
only; Reopen stays the human's. Without `close` (and for every AI Factory that is not Dark) the verdict stays the
human's, as before.

It closes only when all of this holds, read fresh under the workspace's release lock right before it acts: the factory
and Dark switched on, the ledger whole, the epic open, its charter live (not paused, edited or out of budget), Dark,
armed and signed with `close`; the Ready report holds; every release stage the charter signs is proven and not out of
date, and no sensitive path stopped it; no permission card of the epic is open; no Stopped reason; and the coverage
check (epic_coverage_ok). Each condition is read from the ledger, the runner's own records or orch's records behind a
ticket's status, never from a field an agent writes as such.

It gives the verdict through the same Ops path the human's Accept uses (`Ops.verdict(epic, "done")`, which closes
the children too), bound to the hash of exactly the evidence the Ready report showed, as the dashboard's human actor
with `via` "dark-charter", so the signed ledger entry and the events say it was the charter's. Once per delegation: an
intent marker is created exclusively before it acts, so it never closes twice (also not after a Reopen) and a crash in
between leaves the verdict to the human.
"""
from __future__ import annotations

from orch.core import factory_release as fr, factory_sessions as fs
from orch.core.events import Actor

CHARTER_VIA = "dark-charter"
ACTOR = Actor("human", "you", CHARTER_VIA)  # the dashboard's human actor, marked as acting under the signed charter
MESSAGE = "closed by itself under the Dark charter"


def epic_coverage_ok(ws, epic) -> bool:
    """factory_report.coverage_ok(ws, epic) is True: every file the epic names is named by a child (a text check, not
    a check that anything was built). Anything else (False, None when the epic names no file, any other value, an
    error) is not ok (fail closed)."""
    from orch.core import factory_report
    try:
        return factory_report.coverage_ok(ws, epic) is True
    except Exception:
        return False


def _marker(ws, epic_id: str, did: str, kind: str):
    return fr._dir(ws, epic_id) / f"close.{fs._safe(str(did))}.{kind}"


def record(ws, epic_id: str, did: str) -> dict | None:
    """The runner's outcome record of an automatic close under delegation `did`, or None."""
    p = _marker(ws, epic_id, did, "outcome")
    import os
    return (fr._read(p) or {"closed": False, "why": "its record cannot be read"}) if os.path.lexists(p) else None


def charter_status(ws, ticket_id: str, signed=None) -> dict | None:
    """The ticket's newest signed status entry when it is a done verdict given under a charter (via "dark-charter"),
    else None. The one place that answers "was this done the charter's": from the signed ledger, never from an event
    or a ticket field."""
    from orch.core import ledger
    try:
        chain = ledger.status_chain(ws, str(ticket_id), signed)
    except Exception:
        return None
    last = chain[-1] if chain else {}
    ok = last.get("kind") == "verdict" and last.get("verdict") == "done" and last.get("via") == CHARTER_VIA
    return last if ok else None


def closed_by_charter(ws, epic, signed=None) -> bool:
    """Whether the epic is done by a verdict given under the charter (charter_status)."""
    return epic.status == "done" and charter_status(ws, epic.id, signed) is not None


def _b(code: str, text: str, pending: bool = False) -> dict:
    return {"code": code, "text": text, "pending": pending}


def blockers(ws, epic, d, *, signed=None, rep=None) -> list[dict]:
    """Why the epic does not close by itself now: [{code, text, pending}], [] when it closes in the runner's next
    round. `pending`: the runner can get there by itself (a release stage still to run, children still working);
    the others need the human. Never raises: an error is a blocker."""
    import os
    from orch.core import factory_report, ledger, permits
    try:
        if not ledger.head_ok():  # a cut ledger backs no charter at all: said first
            return [_b("ledger", "the approval ledger on this machine is cut")]
        if d is None or not d.get("close"):
            return [_b("charter", "the charter does not sign closing by itself")]
        if not permits.enabled(ws) or not permits.dark_on(ws):
            return [_b("off", "AI Factory or its Dark switch is off")]
        if epic.status != "open":
            return [_b("status", f"the epic is {epic.status}")]
        if not d.get("dark") or not d["active"]:
            return [_b("charter", "the charter is paused, edited since you signed it, or out of budget")]
        if not fs.armed(ws, d["id"]):
            return [_b("unarmed", "the run was not started from the dashboard")]
        if os.path.lexists(_marker(ws, epic.id, d["id"], "intent")):
            return [_b("once", "it closed, or began to close, by itself once already under this charter: the verdict "
                               "is yours now")]
        signed = ledger.entries(ws) if signed is None else signed
        out = []
        reasons = factory_report.stopped(ws, epic, signed=signed)
        if reasons:
            out.append(_b("stopped", "it is Stopped: " + "; ".join(r["label"] for r in reasons)))
        if any(str(r["epic"]).upper() == epic.id.upper() for r in permits.open_requests(ws, signed)):
            out.append(_b("request", "a permission card of this epic is open"))
        rep = factory_report.ready(ws, epic, signed=signed) if rep is None else rep
        if rep is None:
            out.append(_b("ready", "every child is in testing or done, with evidence cited for every criterion",
                          pending=True))
        else:  # the stricter evidence rules an unattended close needs (orch.core.evidence.strict_missing)
            out += _evidence_blockers(ws, rep)
        if d.get("release"):
            st = fr.status(ws, epic, d)
            if st is None or not st["recipe"]:
                out.append(_b("release", "the release recipe cannot be read"))
            else:
                if st["sensitive"] is not None:
                    out.append(_b("release", "a child branch touches a sensitive path"))
                if any(s["name"] == "merge" for s in st["stages"]):
                    for k in fr._units(ws, epic):
                        us = fr.unit_state(ws, epic.id, "merge", k)
                        if us["state"] == "proven" and not fr.own_merge(us):
                            out.append(_b("release", f"the merge of {k} records no commit of its own"))
                for s in st["stages"]:
                    if s.get("held"):
                        out.append(_b("release", "production is held: another epic's production is unresolved ("
                                      + ", ".join(s["held"]) + ")"))
                    elif (s.get("window") or {}).get("why"):
                        out.append(_b("release", s["window"]["why"]))
                    elif _after_budget(s.get("window"), d):
                        out.append(_b("release", "the production window opens only after this charter's time budget "
                                                 "ends, so production will not run under it"))
                    elif s["state"] in ("waiting", "running"):
                        out.append(_b("release", f"the {s['name']} stage is proven by its check", pending=True))
                    elif s["state"] != "proven":
                        out.append(_b("release", f"the {s['name']} stage is {s['state']}"))
        if not epic_coverage_ok(ws, epic):
            out.append(_b("coverage", "the children do not cover the epic's requirements"))
        return out
    except Exception as e:  # fail closed, and say why
        return [_b("error", f"orch could not tell whether everything is proven ({type(e).__name__})")]


def _after_budget(window, d) -> bool:
    """Whether a shut production window opens at or after the charter's time budget ends (then it never runs)."""
    from datetime import timedelta
    from orch import clock
    if not window or window.get("open") or not window.get("opens"):
        return False
    try:
        ends = clock.parse_stamp(str(d.get("at"))) + timedelta(hours=int(d["max_hours"]))
        return clock.parse_stamp(window["opens"]) >= ends
    except (ValueError, TypeError, KeyError, OverflowError):
        return True  # cannot tell: never promise a close that may not come


def _evidence_blockers(ws, rep) -> list[dict]:
    from orch.core import evidence, store
    from orch.core.factory_report import _text
    out = []
    for row in rep["children"]:
        if row["status"] != "testing":
            continue
        t = store.read_ticket(store.resolve(ws, row["id"]).path)
        for n, why in evidence.strict_missing(t):
            out.append(_b("evidence", f"the evidence of {row['id']}"
                                      + (f" for AC{n}" if n else "") + " does not meet the close rules: "
                                      + _text(why, 160)))
    return out


def view(ws, epic, d, *, signed=None, rep=None) -> dict | None:
    """What the run view and the Ready card show for a charter that signs `close`, else None: {closed: the outcome
    record, by_charter, blockers, pending (every blocker is one the runner gets past by itself)}."""
    if not d or not d.get("close"):
        return None
    rec = record(ws, epic.id, d["id"])
    by = closed_by_charter(ws, epic, signed)
    bl = [] if by else blockers(ws, epic, d, signed=signed, rep=rep)
    return {"closed": rec, "by_charter": by, "blockers": bl, "pending": all(b["pending"] for b in bl)}


def tick(ws, actor) -> list[str]:
    """One auto-close round over every open epic whose charter signs `close`. Only a human process (the dashboard
    the human started) runs it; it does nothing unless the factory is on."""
    from orch.core import epics, permits, store
    fs.human_check(actor, "closing an epic under its charter")
    if not permits.enabled(ws) or not permits.dark_on(ws):
        return []
    lines = []
    for entry in store.scan(ws):
        if entry.meta is None or not epics.is_epic(entry.meta) or entry.status != "open":
            continue
        epic = fr._ticket(ws, entry.id)
        d = permits.factory_delegation(ws, epic) if epic is not None else None
        if d is None or not d.get("close") or blockers(ws, epic, d):
            continue
        line = close_once(ws, entry.id, d["id"])
        if line:
            lines.append(line)
    return lines


def close_once(ws, epic_id: str, did: str) -> str | None:
    """Give the epic's done verdict under the charter, once: under the release lock, every condition read again,
    the intent marker created exclusively, then the same Ops verdict the human's Accept gives, bound to the Ready
    report's hash; then the outcome record and a `verdict.auto` event (the children closed and the hash only)."""
    from orch import clock
    from orch.core import factory_report, ledger, permits
    from orch.core.events import append_event
    from orch.core.ops import Ops
    from orch.errors import OrchError
    fs.human_check(ACTOR, "closing an epic under its charter")  # never under an agent harness
    if not fr.acquire(ws, epic_id, 120):
        return None  # a release holds the lock: the next round looks again
    try:
        epic = fr._ticket(ws, epic_id)
        signed = ledger.entries(ws)
        d = permits.factory_delegation(ws, epic, signed) if epic is not None else None
        if d is None or d["id"] != did:
            return None
        rep = factory_report.ready(ws, epic, signed=signed)
        if rep is None or blockers(ws, epic, d, signed=signed, rep=rep):
            return None
        kids = [r["id"] for r in rep["children"] if r["status"] == "testing"]
        if not fs._create(_marker(ws, epic.id, did, "intent"), {"at": clock.stamp_s(), "seen": rep["seen"]}):
            return None  # another round or dashboard took it: never twice
        stages = [s["name"] for s in (fr.status(ws, epic, d) or {}).get("stages", [])]
        try:
            Ops(ws, ACTOR).verdict(epic.id, "done", MESSAGE, expected_hash=rep["seen"], delegation=did)
        except OrchError as e:
            fs._create(_marker(ws, epic.id, did, "outcome"),
                       {"closed": False, "at": clock.stamp_s(), "why": e.message[:300]})
            return f"{epic.id}: not closed by itself: {e.message}"
        fs._create(_marker(ws, epic.id, did, "outcome"),
                   {"closed": True, "at": clock.stamp_s(), "children": kids, "proven": rep["proven"],
                    "total": rep["total"], "stages": stages})
        append_event(ws, epic.id, "verdict.auto", ACTOR, {"children": kids, "seen": rep["seen"], "delegation": did})
        return f"{epic.id}: closed by itself under its charter with {', '.join(kids)}"
    finally:
        fr.release_lock(ws)
