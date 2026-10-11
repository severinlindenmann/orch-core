"""Ticket rows and documents of contract v1.3.1 as the UI's TicketSummary, TicketDocument and OrchEvent.

Rules: unwrap the host data, never parse `lines`, spell statuses the UI's way, and where the host does not serve a field
(people other than owner and assignees, links, visibility, task receipts, verdict, the full artifact manifest) leave it
empty and name it in `unavailable`, so the page can say "not available" instead of showing a made-up value.
"""

from typing import Any

# The six statuses the UI can draw. A host `closed` ticket has no place in the UI yet (see HANDOVER, M2 concerns).
UI_STATUSES = ("backlog", "open", "in-progress", "waiting", "testing", "done")
HOST_BOARD_STATUSES = ("backlog", "open", "in_progress", "testing", "done")
GATES = ("requirements", "plan", "verify", "code")
TASK_STATE = {"open": "todo", "started": "doing", "done": "done", "skipped": "skipped", "blocked": "blocked"}
NOT_SERVED = [
    "task receipts",
    "verdict",
    "artifact manifest",
    "gate approvals",
    "branch commits",
    "claim details",
    "section history",
]


def host_status(status: str) -> str:
    return status.replace("-", "_")


def board_statuses() -> list[str]:
    return list(HOST_BOARD_STATUSES)


def _ui_status(status: str) -> str:
    return status.replace("_", "-")


def _needs_by_uid(needs: dict | None, viewer: str | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in (needs or {}).get("items", []):
        if item.get("uid") and (viewer is None or viewer in item.get("who", [])):
            out.setdefault(item["uid"], []).append(item)
    return out


def summary(row: dict, viewer: str | None = None, mine: dict[str, list[dict]] | None = None) -> dict:
    """One `ticket.query` row. `mine` maps a ticket uid to the viewer's `needs` items, which decide the turn."""
    claim = row.get("claim")
    own = (mine or {}).get(row["uid"], [])
    kinds = list(row.get("needs", []))
    if own:
        turn = {"who": viewer, "why": _why(own[0])}
    elif claim:
        turn = {"who": f"agent:{claim.get('agent') or claim['session']}", "why": "An agent holds the claim"}
    else:
        turn = {"who": "nobody", "why": ""}
    open_gate = next((g for g in GATES if row.get("gates", {}).get(g) == "open"), None)
    return {
        "key": row["key"],
        "uid": row["uid"],
        "title": row["title"],
        "type": row["type"],
        "priority": row.get("priority") or "medium",
        "size": row.get("size"),
        "status": _ui_status(row["status"]),
        "labels": list(row.get("labels", [])),
        "parent": row.get("parent"),
        "owner": row.get("owner"),
        "assignees": list(row.get("assignees", [])),
        "claim": {"agent": claim.get("agent"), "for": None, "session": claim["session"]} if claim else None,
        "turn": turn,
        # Row counts are not served: zero, so no progress bar is drawn.
        "progress": {"tasks_done": 0, "tasks_total": 0, "ac_proven": 0, "ac_total": 0},
        "open_questions": kinds.count("question"),
        "blocking_questions": 0,
        "restricted": False,
        "awaiting_gate": open_gate if "approve" in kinds else None,
        "addons": {},
        "updated_at": row.get("updated_at") or "",
        "head_seq": row.get("head_seq"),
        "epic": row.get("epic"),
    }


def _why(item: dict) -> str:
    return {
        "question": "A question is waiting for your answer",
        "approve": "A gate is waiting for your approval",
        "no_eligible": "No eligible approver for a gate",
        "acknowledge": "An invalid event needs your acknowledgement",
    }.get(item["kind"], "Waiting for you")


def page(data: dict, needs: dict | None, viewer: str | None) -> dict:
    """The `ticket.query` result as one page of summaries; `next_page` drives the client's paging."""
    mine = _needs_by_uid(needs, viewer)
    return {
        "rows": [summary(r, viewer, mine) for r in data["rows"]],
        "next_page": data.get("next_page"),
        "total": data["total"],
        "sub_cursor": data.get("sub_cursor"),
        "as_of": data.get("as_of"),
    }


def _sections(doc: dict) -> dict[str, str]:
    body = {k: v for k, v in (doc.get("sections") or {}).items() if v}
    if doc.get("current_state"):
        body["current_state"] = doc["current_state"]
    return body


def events(data: dict) -> list[dict]:
    """`ticket.get` log items as OrchEvents. The summary is host text, shown as data; no payload fields are invented."""
    out = []
    for e in data["log"]:
        actor = e["actor"]
        ui_actor = {"kind": actor["kind"], "id": actor.get("id") or ""}
        if actor["kind"] == "host":
            ui_actor["id"] = "orch"
        out.append(
            {
                "v": 2,
                "id": f"{e['seq']}",
                "seq": e["seq"],
                "at": e["at"],
                "type": e["type"],
                "actor": ui_actor,
                "summary": e["summary"],
            }
        )
    return out


def document(data: dict, viewer: str | None = None, mine: dict[str, list[dict]] | None = None) -> dict:
    """`ticket.get` as a TicketDocument. The F1 section 7 document carries the ticket.json fields, so no second read."""
    doc, log = data["doc"], data["log"]
    complete = not data.get("log_more", False) and bool(log)
    uid = doc["uid"]
    own = (mine or {}).get(uid, [])
    claim = doc.get("claim")
    people = doc.get("people") or {}
    turn = (
        {"who": viewer, "why": _why(own[0])}
        if own
        else {"who": "nobody", "why": ""}
        if not claim
        else {"who": "agent:" + claim["session"], "why": "An agent holds the claim"}
    )
    evidence = {c["id"]: c["evidence"] for c in data.get("criteria", [])}
    asked = {q["id"]: q for q in data.get("questions", [])}
    answer_at = {e["seq"]: e["at"] for e in log}
    questions = []
    for q in doc["questions"]:
        entry = {"id": q["id"], "to": q["to"], "text": q["text"], "blocking": q["blocking"]}
        if q.get("options"):
            entry["options"] = [{"key": k, "label": k} for k in q["options"]]
        questions.append(entry)
    questions_state = []
    for q in questions:
        info = asked.get(q["id"], {})
        answered = info.get("state", "open") != "open"
        questions_state.append(
            {
                **q,
                "state": "answered" if answered else "open",
                "asked_at": "",
                "asked_by": "",
                "answer": {"by": info["answered_by"], "at": answer_at.get(info.get("answered_seq"), "")}
                if answered and info.get("answered_by")
                else None,
            }
        )
    tasks = [{"id": t["id"], "text": t["text"], "verify": None, "proves": list(t["proves"])} for t in doc["tasks"]]
    acceptance = [{"id": a["id"], "text": a["text"]} for a in doc["acceptance"]]
    artifacts, seen = [], set()
    for crit in data.get("criteria", []):
        for ev in crit["evidence"]:
            if ev["artifact"] in seen:
                continue
            seen.add(ev["artifact"])
            artifacts.append(
                {
                    "name": ev["artifact"],
                    "kind": ev["kind"] if ev["kind"] in KINDS else "other",
                    "bytes": 0,
                    "sha256": ev["digest"],
                    "ac": crit["id"],
                    "added_by": ev["attested_by"],
                    "at": "",
                }
            )
    visibility = doc.get("visibility", "workspace")
    return {
        "schema": "orch.ticket/2",
        "uid": uid,
        "key": doc["key"],
        "title": doc["title"],
        "type": doc["type"],
        "priority": doc.get("priority") or "medium",
        "size": doc.get("size"),
        "labels": list(doc.get("labels", [])),
        "parent": doc.get("parent"),
        "blocked_by": list(doc.get("blocked_by", [])),
        "due": doc.get("due"),
        "visibility": visibility,
        "links": doc.get("links") or {"repos": [], "branches": {}, "prs": [], "external": []},
        "acceptance": acceptance,
        "tasks": tasks,
        "questions": questions,
        "addons": {},
        "status": _ui_status(doc["status"]),
        "people": {
            "owner": doc.get("owner"),
            "assignees": list(people.get("assignees", [])),
            "reviewers": list(people.get("reviewers", [])),
            "watchers": list(people.get("watchers", [])),
        },
        "claim": {
            "agent": "",
            "session": claim["session"],
            "for": "",
            "grant": "",
            "since": "",
            "expires": "",
            "live": claim["live"],
        }
        if claim
        else None,
        "tasks_state": [
            {**tasks[i], "state": TASK_STATE.get(t["state"], "todo"), "lease": None, "receipt": None, "done_at": None}
            for i, t in enumerate(doc["tasks"])
        ],
        "acceptance_state": [
            {
                **a,
                "state": "proven" if evidence.get(a["id"]) or src["evidence"] else "unproven",
                "evidence": _evidence(src["evidence"]),
            }
            for a, src in zip(acceptance, doc["acceptance"], strict=True)
        ],
        "questions_state": questions_state,
        "gates": {g: _gate(data["gates"].get(g)) for g in GATES},
        "artifacts": artifacts,
        "verdict": None,
        "turn": turn,
        "body": _sections(doc),
        "head": {"seq": log[-1]["seq"] if complete else 0, "hash": ""},
        "created_at": log[0]["at"] if log and log[0]["type"] == "ticket.created" else "",
        "updated_at": log[-1]["at"] if complete else "",
        "restricted": visibility != "workspace",
        "branch": None,
        "unavailable": NOT_SERVED,
        "log_more": data.get("log_more", False),
    }


KINDS = {"screenshot", "log", "report", "link", "dataset", "build", "diagram", "receipt", "feedback", "other"}


def _evidence(refs: list[str]) -> list[dict]:
    out = []
    for ref in refs:
        kind, _, name = ref.partition(":")
        if kind in ("artifact", "task") and name:
            out.append({"kind": kind, "ref": name})
    return out


def _gate(gate: dict[str, Any] | None) -> dict:
    """A gate as the UI draws it. Approvals and the policy text are not served in `ticket.get` yet."""
    state = (gate or {}).get("state", "n/a")
    return {
        "state": "approved" if state == "approved" else "pending",
        "approvals": [],
        "needed": 0,
        "approvers": "",
        "hash": (gate or {}).get("hash12"),
        "required": state != "n/a",
        "gen": (gate or {}).get("gen"),
    }
