"""AI Factory phase 3 (#2): the Ready report and the Stopped message of a factory epic.

Both are derived, read-only views. Nothing here writes, signs, approves, grants or starts anything, and nothing an
agent can write turns one on or hides it: they come from the epic's signed charter, the signed ledger entries (the
human's send-backs and denials), the budget markers beside the ledger and the tickets' own state.

- Ready: every child is in testing or done, at least one is in testing, and every criterion of every child in testing
  has evidence. The human's one action on it is the epic verdict that already exists (`orch verdict <epic> done`, the
  epic page): it signs the epic's verdict hash, so the hash the report carries is the one `epics.verdict_hash` gives.
  The report itself accepts nothing and never closes a child.
- Stopped: a dead end the agents cannot get out of by themselves, with the reasons: the budget is used up, a child
  was sent back by the human FAILED_TRIES times, a request the human denied still holds a child back, or the ledger on
  this machine was cut. It says what the human can do; it offers no action of its own.

Only while `factory.enabled` is on (as the permission cards), judged by the signed charter alone.
"""
from __future__ import annotations

from orch.core import epics, evidence, permits, store

FAILED_TRIES = 3  # signed send-backs of one child after which the factory counts as stuck on it
_EXCERPT = 400


def _text(s, limit: int = _EXCERPT) -> str:
    """Agent text for the report as one plain line: characters outside printable ASCII escaped, never rendered as
    Markdown (the template's own escaping covers HTML)."""
    line = " ".join(str(s or "").split())[:limit]
    return "".join(c if 32 <= ord(c) < 127 else c.encode("unicode_escape").decode("ascii") for c in line)


def _kids(ws, epic, entries):
    """[(entry, Ticket)] of the epic's children; None when one cannot be read (a report never guesses)."""
    out = []
    for e in epics.children(ws, epic.id, entries):
        try:
            out.append((e, store.read_ticket(e.path)))
        except Exception:
            return None
    return out


def ready(ws, epic, *, entries=None, signed=None, events=None) -> dict | None:
    """The Ready report of factory epic `epic`, or None while it is not ready."""
    if not permits.enabled(ws) or epic.status != "open":
        return None
    from orch.core.gates import invalidated_gates
    d = permits.factory_delegation(ws, epic, signed)
    if d is None or d["epic_changed"]:
        return None
    kids = _kids(ws, epic, entries)
    if not kids or any(e.status not in ("testing", "done") for e, _ in kids):
        return None
    testing = [t for e, t in kids if e.status == "testing"]
    if not testing or any(invalidated_gates(t) for t in testing):
        return None
    rows = []
    for e, t in kids:
        proven, total = evidence.progress(t)
        if e.status == "testing" and (total == 0 or proven != total):
            return None
        rows.append({"id": t.id, "title": t.title, "status": e.status, "size": t.meta.get("size"),
                     "how": epics.STATE_LABELS[epics.child_state(ws, epic, t, signed, events, entries)],
                     "proven": proven, "total": total, "verification": _text(t.section("Verification")),
                     "findings": _text(t.section("Findings")),
                     "where": [_text(r) for r in (t.meta.get("repos") or []) if isinstance(r, str)][:5]})
    return {"epic": epic.id, "title": epic.title, "children": rows, "open": len(testing),
            "seen": epics.verdict_hash(testing, ws), "proven": sum(r["proven"] for r in rows),
            "total": sum(r["total"] for r in rows)}


def _ledger_cut_epic(epic, kids) -> bool:
    """A cut ledger backs no charter, so the signed record cannot say which epics are factories: an epic with a
    child whose gate names a delegation counts (only consulted while the ledger is cut)."""
    return any(isinstance(g, dict) and g.get("delegation")
               for _, t in kids for g in (t.meta.get("gates") or {}).values())


def stopped(ws, epic, *, entries=None, signed=None, events=None) -> list[dict]:
    """Why factory epic `epic` is at a dead end: [{code, label, text}], empty while it is not (also empty for a done
    epic and for one that is Ready)."""
    if not permits.enabled(ws) or epic.status in ("backlog", "done"):
        return []
    from orch.core import ledger
    if not ledger.head_ok():
        kids = _kids(ws, epic, entries) or []
        if kids and _ledger_cut_epic(epic, kids):
            return [{"code": "ledger-cut", "label": "Ledger cut",
                     "text": "the approval ledger on this machine is shorter than its signed head, so no approval or "
                             "grant counts (`orch check` reports ledger-cut)"}]
        return []
    d = permits.factory_delegation(ws, epic, signed)
    if d is None or ready(ws, epic, entries=entries, signed=signed, events=events):
        return []
    signed = permits._signed(ws, signed)
    out = []
    reason = permits.budget_reason(ws, epic, d, events)
    if reason:
        out.append({"code": "budget", "label": "Budget used up", "text": reason})
    kids = {e.id: e for e in epics.children(ws, epic.id, entries)}
    sent_back: dict[str, int] = {}
    for x in signed:
        if x.get("kind") == "verdict" and x.get("verdict") == "follow-up" and x.get("ticket") in kids:
            sent_back[x["ticket"]] = sent_back.get(x["ticket"], 0) + 1
    for tid, n in sent_back.items():
        if n >= FAILED_TRIES and kids[tid].status != "done":
            out.append({"code": "sent-back", "label": "Sent back repeatedly",
                        "text": f"{tid} was sent back {n} times and is not finished"})
    denied = permits.decisions(ws, signed)
    for r in permits.requests(ws, events).values():
        if r["epic"] != epic.id or r["ticket"] not in kids or kids[r["ticket"]].status in ("testing", "done"):
            continue
        if denied.get((r["id"], r["sha"]), {}).get("kind") == "permit_deny" and not any(
                g.get("kind") == "grant" and g.get("epic") == epic.id and g.get("command_sha") == r["sha"]
                for g in signed):
            out.append({"code": "denied", "label": "Permission denied",
                        "text": f"you denied {r['id']} and {r['ticket']} has not finished since"})
    return out


def _epics(ws, entries):
    for e in store.scan(ws) if entries is None else entries:
        if e.status != "done" and e.meta is not None and epics.is_epic(e.meta):
            try:
                yield store.read_ticket(e.path)
            except Exception:
                continue


def cards(ws, entries=None, events=None) -> dict:
    """{ready: [report], stopped: [{epic, title, reasons}]} over every factory epic; both empty while the factory is
    off. Memoised per request scope."""
    if not permits.enabled(ws):
        return {"ready": [], "stopped": []}

    def compute():
        from orch.core import ledger
        signed = ledger.entries(ws)
        out = {"ready": [], "stopped": []}
        for epic in _epics(ws, entries):
            rep = ready(ws, epic, entries=entries, signed=signed, events=events)
            if rep:
                out["ready"].append(rep)
                continue
            why = stopped(ws, epic, entries=entries, signed=signed, events=events)
            if why:
                out["stopped"].append({"epic": epic.id, "title": epic.title, "reasons": why})
        return out
    return store.memo(ws, "factory-cards", compute)


def items(ws, entries=None, events=None) -> list[dict]:
    """The waiting-on-the-human items of query.waiting(): one per Ready report and one per Stopped epic."""
    c = cards(ws, entries, events)
    out = [{"ticket": r["epic"], "title": r["title"], "status": "open", "kind": "factory-ready",
            "detail": f"{r['open']} in testing", "scope": "blocking", "priority": None} for r in c["ready"]]
    out += [{"ticket": s["epic"], "title": s["title"], "status": "open", "kind": "factory-stopped",
             "detail": s["reasons"][0]["label"].lower(), "scope": "blocking", "priority": None} for s in c["stopped"]]
    return out


def signal(ws, ticket) -> str | None:
    """"ready" | "stopped" when `ticket` is a factory epic (or a child of one) in that state, else None: what
    `orch wait` wakes on. Never raises."""
    try:
        epic = permits.charter_epic(ws, ticket)
        if epic is None or not permits.enabled(ws):
            return None
        c = cards(ws)
        key = epic.id.upper()
        if any(r["epic"].upper() == key for r in c["ready"]):
            return "ready"
        return "stopped" if any(s["epic"].upper() == key for s in c["stopped"]) else None
    except Exception:
        return None
