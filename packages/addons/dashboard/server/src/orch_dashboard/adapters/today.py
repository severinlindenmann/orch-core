"""`needs` and the ticket rows as the UI's TodayDocument."""

from datetime import datetime

from .tickets import summary, ui_actor

KIND = {
    "question": "question",
    "approve": "approval",
    "no_eligible": "no_eligible",
    "acknowledge": "acknowledge",
    "addon": "addon",
}


# Core's words for what each gate asks of a person; the host sends only the gate name.
ASK = {
    "requirements": "Approve the requirements",
    "plan": "Approve the plan",
    "verify": "Give a verdict",
    "code": "Review the code",
}


NO_ELIGIBLE = {
    # `no_token`: the policy names no approver token at all. `no_visible_person`: some do, but none can see the ticket.
    "no_token": "The {gate} gate has no approver",
    "no_visible_person": "No one who can see this ticket can approve the {gate} gate",
}


# What Today lists as "needs you" (and so what every badge counts): questions, approvals and verdicts. Notices
# (`no_eligible`, `acknowledge`, `addon`) are shown on their own rows but are not counted (RH-6).
ACTIONABLE = {"question", "approve"}


def actionable_count(items: list[dict], role: str | None = None) -> int:
    """The workspace's "needs you" number: exactly the rows Today's "N need you" counts. A viewer decides nothing."""
    return 0 if role == "viewer" else sum(1 for n in items if n["kind"] in ACTIONABLE)


def ask(n: dict) -> str:
    """The line a row shows. The question text is a Snip of the ticket's own (the page reads the full one)."""
    if n["kind"] == "approve":
        return ASK.get(n.get("ref") or "", "Approve")
    if n["kind"] == "no_eligible":
        gate = n.get("ref") or "gate"
        cause = (n.get("payload") or {}).get("cause")
        return NO_ELIGIBLE[cause].format(gate=gate) if cause in NO_ELIGIBLE else f"No one can approve the {gate} gate"
    if n["kind"] == "question":
        return n.get("text") or ""
    if n["kind"] == "addon":
        return n.get("text") or n.get("detail") or ""  # the rule's own text (at most 120 characters)
    return n.get("detail") or ""


def item(n: dict, titles: dict[str, str]) -> dict:
    kind = "verdict" if n["kind"] == "approve" and n.get("ref") == "verify" else KIND[n["kind"]]
    out = {
        "kind": kind,
        "ticket": n.get("key") or "",
        "uid": n.get("uid"),
        "title": titles.get(n.get("uid") or "", ""),
        "text": ask(n),
        "ref": n.get("ref"),
        # When the need arose: '' = unknown (the host leaves `since` out for acknowledge and for what it cannot date).
        "since": n.get("since") or "",
        "blocking": n["kind"] == "question" and n.get("detail") == "blocking",
    }
    if n.get("text_cut"):
        out["cut"] = True  # a Snip: shown as shortened; the ticket page has the full question
    if n.get("status") == "closed":
        out["closed"] = True  # an open question on a closed ticket is still listed (v1.4 section 0.8.4)
    return out


TERMINAL_WAIT = "Agent may need you: check its terminal"
#: run-10 J04: the host saw the agent inside `orch wait` (source "wait"): it waits for a decision, not at its prompt
AGENT_WAITS = "Agent is waiting for the next step"
SOURCES = ("wait", "hook", "quiet")


def _at(value) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


FINISHED = ("done", "closed", "cancelled")


def terminal_waits(runs: list[dict], rows: list[dict], mine: dict[str, list[dict]]) -> list[dict]:
    """Run-9 feedback 3.6: an agent run the host marks `waiting` (Notification hook, else a quiet terminal) on a ticket
    where nothing is open for the person. Local and display only; it is not a need (never counted). `ref` is the run id:
    "Open terminal" attaches to it. This is the dashboard's one "agent idle" signal (run-9 J04): a ticket event after
    the marker does NOT hide it (the person's own approval is such an event, and the idle agent stays idle); the host
    clears `waiting` itself as soon as the agent prints again. `source` says how the host knows: "wait" (the agent is
    inside `orch wait`, whatever its terminal prints: run-10 J04), "hook" or "quiet" (fallbacks outside orch wait)."""
    by_key = {r["key"]: r for r in rows if "key" in r}
    out = []
    for r in runs:
        w = r.get("waiting")
        row = by_key.get(r.get("ticket") or "")
        if not isinstance(w, dict) or row is None or mine.get(row.get("uid")):
            continue
        if row.get("status") in FINISHED:  # nothing is waiting on a finished ticket (run-11 P06)
            continue
        if _at(w.get("since")) is None:
            continue
        source = w.get("source") if w.get("source") in SOURCES else None
        out.append(
            {
                "kind": "terminal_wait",
                "ticket": row["key"],
                "uid": row["uid"],
                "title": row["title"],
                "text": AGENT_WAITS if source == "wait" else TERMINAL_WAIT,
                "ref": r["run"],
                "since": w["since"],
                "blocking": False,
                **({"source": source} if source else {}),
            }
        )
    return out


def recent_row(a: dict) -> dict:
    """One activity item as a Today row. `jpos`, `ws_seq`, `log` and `uid` never reach the browser."""
    return {
        "seq": a["seq"],
        "at": a["at"],
        "type": a["type"],
        "actor": ui_actor(a["actor"]),
        "ticket": a.get("key") or "",
        "title": a.get("title") or "",
        "summary": a["summary"],
        "id": a["id"],
    }


def today(
    needs: dict,
    rows: list[dict],
    ws: str,
    now: str,
    viewer: str,
    role: str,
    recent: list[dict] | None = None,
    closed: list[dict] | None = None,
    runs: list[dict] | None = None,
) -> dict:
    titles = {r["uid"]: r["title"] for r in [*rows, *(closed or [])]}
    mine = {}
    for n in needs["items"]:
        if n.get("uid"):
            mine.setdefault(n["uid"], []).append(n)
    items = [item(n, titles) for n in needs["items"]]
    waits = terminal_waits(runs or [], rows, mine)
    idle = {w["uid"] for w in waits}
    counts: dict[str, int] = {}
    for r in rows:
        status = r["status"].replace("_", "-")
        counts[status] = counts.get(status, 0) + 1
    waiting = [r for r in rows if r.get("needs") and r["uid"] not in mine]
    viewer_role = role == "viewer"
    return {
        "now": now,
        "workspace": ws,
        "needs_you": [] if viewer_role else [*items, *waits],
        "read_only_open": items if viewer_role else [],
        "waiting_on_others": {"count": len(waiting), "people": []},
        # an agent that is idle at its prompt is not working (run-9 J04): its ticket is in needs_you as terminal_wait
        "working": [summary(r, viewer, mine) for r in rows if r["status"] == "in_progress" and r["uid"] not in idle],
        "recent": [recent_row(a) for a in recent or []],
        "counts": counts,
        "sub_cursor": needs.get("sub_cursor"),
    }
