"""`needs` and the ticket rows as the UI's TodayDocument."""

from .tickets import summary

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


def ask(n: dict) -> str:
    """The line a row shows. A question's own text is the ticket's (the page reads it there); the host sends none."""
    if n["kind"] == "approve":
        return ASK.get(n.get("ref") or "", "Approve")
    if n["kind"] == "no_eligible":
        return f"No one can approve the {n.get('ref') or 'gate'} gate"
    if n["kind"] == "question":
        return ""
    return n.get("detail") or ""


def item(n: dict, titles: dict[str, str]) -> dict:
    kind = "verdict" if n["kind"] == "approve" and n.get("ref") == "verify" else KIND[n["kind"]]
    return {
        "kind": kind,
        "ticket": n.get("key") or "",
        "uid": n.get("uid"),
        "title": titles.get(n.get("uid") or "", ""),
        "text": ask(n),
        "ref": n.get("ref"),
        # When the need arose is not served: empty, not the ticket's update time.
        "since": "",
        "blocking": n["kind"] == "question" and n.get("detail") == "blocking",
    }


def today(needs: dict, rows: list[dict], ws: str, now: str, viewer: str, role: str) -> dict:
    titles = {r["uid"]: r["title"] for r in rows}
    mine = {}
    for n in needs["items"]:
        if n.get("uid"):
            mine.setdefault(n["uid"], []).append(n)
    items = [item(n, titles) for n in needs["items"]]
    counts: dict[str, int] = {}
    for r in rows:
        status = r["status"].replace("_", "-")
        counts[status] = counts.get(status, 0) + 1
    waiting = [r for r in rows if r.get("needs") and r["uid"] not in mine]
    viewer_role = role == "viewer"
    return {
        "now": now,
        "workspace": ws,
        "needs_you": [] if viewer_role else items,
        "read_only_open": items if viewer_role else [],
        "waiting_on_others": {"count": len(waiting), "people": []},
        "working": [summary(r, viewer, mine) for r in rows if r["status"] == "in_progress"],
        "recent": [],
        "counts": counts,
        "sub_cursor": needs.get("sub_cursor"),
    }
