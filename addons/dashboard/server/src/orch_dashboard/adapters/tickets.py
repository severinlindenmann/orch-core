"""Ticket rows and documents of contract v1.4 as the UI's TicketSummary, TicketDocument and OrchEvent.

Rules: unwrap the host data, never parse `lines`, spell statuses the UI's way, and where the host does not serve a field
(people other than owner and assignees, links, visibility, task receipts, verdict, the full artifact manifest) leave it
empty and name it in `unavailable`, so the page can say "not available" instead of showing a made-up value.
"""

import re
from typing import Any

# The statuses the UI can draw. `closed` is a state of its own (v1.4 section 0.8.4), never a board column.
UI_STATUSES = ("backlog", "open", "in-progress", "waiting", "testing", "done", "closed")
HOST_BOARD_STATUSES = ("backlog", "open", "in_progress", "testing", "done")
GATES = ("requirements", "plan", "verify", "code")
# v1.4 tasks have no "started" state: a task is "doing" while a session holds its lease (`leased_by`).
TASK_STATE = {"open": "todo", "done": "done", "skipped": "skipped", "blocked": "blocked"}
# What no read serves, whatever the host (v1.4 stores hashes, not texts or commits). Named so the page says "not
# available" instead of showing a made-up value.
NEVER_SERVED = ["branch commits", "section texts of old revisions", "full text of long comments and verdicts"]
# What only a host with `read_ext/1` serves (absent fields there mean unknown, never zero).
NEEDS_READ_EXT = [
    "task receipts",
    "verdict",
    "artifact manifest",
    "gate approvals",
    "claim details",
    "question details",
    "section history",
]
NOT_SERVED = NEVER_SERVED + NEEDS_READ_EXT  # a host without read_ext/1
PRESENCE = {"passphrase": "password", "secure-enclave": "touchid", "convenience": "convenience"}
# An auth value this adapter does not know is shown as it is (H5c: never relabelled as "password").
AUTH_RAW = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


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


def _actor_name(actor: dict | None) -> str:
    """An Actor as the UI's string: a person id, an agent's id, `orch` for the host."""
    if not actor:
        return ""
    if actor["kind"] == "host":
        return "orch"
    return actor.get("id") or actor.get("for") or ""


def _claim(claim: dict | None) -> dict | None:
    if not claim:
        return None
    return {
        "agent": claim.get("agent") or "",
        "for": claim.get("for") or "",  # '' = the host did not say (no read_ext/1)
        "session": claim["session"],
        "live": claim.get("live"),  # None = unknown; False = lapsed, not yet released
    }


def summary(row: dict, viewer: str | None = None, mine: dict[str, list[dict]] | None = None) -> dict:
    """One `ticket.query` row. `mine` maps a ticket uid to the viewer's `needs` items, which decide the turn.

    A field the host did not send (no `read_ext/1`) is `None`: unknown, not zero and not false."""
    claim = row.get("claim")
    own = (mine or {}).get(row["uid"], [])
    kinds = list(row.get("needs", []))
    if own:
        turn = {"who": viewer, "why": _why(own[0])}
    elif claim and claim.get("live") is False:
        turn = {"who": "nobody", "why": "The agent's claim has lapsed"}
    elif claim:
        turn = {"who": f"agent:{claim.get('agent') or claim['session']}", "why": "An agent holds the claim"}
    else:
        turn = {"who": "nobody", "why": ""}
    open_gate = next((g for g in GATES if row.get("gates", {}).get(g) == "open"), None)
    tasks, criteria = row.get("tasks"), row.get("criteria")
    out = {
        "key": row["key"],
        "uid": row["uid"],
        "title": row["title"],
        "type": row["type"],
        "priority": row.get("priority"),
        "size": row.get("size"),
        "status": _ui_status(row["status"]),
        "labels": list(row.get("labels", [])),
        "parent": row.get("parent"),
        "owner": row.get("owner"),
        "assignees": list(row.get("assignees", [])),
        "claim": _claim(claim),
        "turn": turn,
        "progress": {
            "tasks_done": tasks["done"],
            "tasks_total": tasks["total"],
            "ac_proven": criteria["done"],
            "ac_total": criteria["total"],
        }
        if tasks is not None and criteria is not None
        else None,
        "open_questions": kinds.count("question"),
        "blocking_questions": row.get("blocking_questions"),
        "restricted": row.get("restricted"),
        "awaiting_gate": open_gate if "approve" in kinds else None,
        "addons": {},
        "updated_at": row.get("updated_at") or "",
        "head_seq": row.get("head_seq"),
        "epic": row.get("epic"),
    }
    if row.get("closed"):
        out["closed"] = _closed(row["closed"])
    return out


def _closed(c: dict) -> dict:
    """`duplicate_of` may name a ticket the viewer cannot see: a plain key, never a link (the UI draws text)."""
    out = {"resolution": c["resolution"], "duplicate_of": c.get("duplicate_of"), "at": c["at"], "by": c.get("by") or ""}
    if "text" in c:
        out["text"] = c["text"]
    return out


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


CORE_KEYS = frozenset({"v", "id", "seq", "at", "type", "actor", "summary", "data", "truncated"})


def ui_actor(actor: dict) -> dict:
    """The host's Actor as the UI's. The host is `orch`; an addon keeps its id; an agent carries `for` and `session`."""
    kind = actor["kind"]
    if kind == "host":
        return {"kind": "host", "id": "orch"}
    if kind == "agent":
        out = {
            "kind": "agent",
            "id": actor.get("id") or "",
            "session": actor.get("session") or "",
            "for": actor.get("for") or "",
        }
        if actor.get("grant"):
            out["grant"] = actor["grant"]
        return out
    out = {"kind": kind, "id": actor.get("id") or ""}
    if kind == "person" and actor.get("device"):
        out["device"] = actor["device"]
    return out


def event(e: dict, ident: str | None = None, can=None) -> dict:
    """A `LogItem` (or the same fields of an activity item) as an OrchEvent.

    `data` is nested in v1.4 and closed per type; its fields are flattened beside the core keys (a field never replaces
    one of them). A Snip field has a `<field>_cut` sibling and no "..." appended: `truncated` lists the cut fields."""
    data = e.get("data") or {}
    out = {
        **{k: v for k, v in data.items() if k not in CORE_KEYS and not k.endswith("_cut")},
        "v": 2,
        "id": ident or f"{e['seq']}",
        "seq": e["seq"],
        "at": e["at"],
        "type": e["type"],
        "actor": ui_actor(e["actor"]),
        "summary": e["summary"],
    }
    from .activity import manual_acceptance

    out.pop("manual_acceptance", None)  # only the line below sets it, never a payload field of the same name
    if (manual := manual_acceptance(e, can)) is not None:  # an explicit marker, never a guess from the summary text
        out["manual_acceptance"] = manual
    if e["type"] == "verdict.given" and "outcome" in data:
        out["result"] = data["outcome"]  # the mock's name for it
    cut = sorted(k[: -len("_cut")] for k, v in data.items() if k.endswith("_cut") and v is True)
    if cut:
        out["truncated"] = cut
    return out


def events(data: dict, can=None) -> list[dict]:
    """`ticket.get` log items as OrchEvents. The summary is host text, shown as data."""
    return [event(e, can=can) for e in data["log"]]


def _sources(raw: list[dict] | None) -> list[dict]:
    return [{"repo": s["repo"], "ref": s.get("ref") or "", "sha": s["sha"]} for s in raw or []]


def _hex(digest: str | None) -> str:
    return (digest or "").removeprefix("sha256:")


def _approval(d: dict) -> dict:
    out = {"by": d["person"], "at": d.get("at") or ""}
    auth = d.get("auth")
    if auth:
        # custody metadata, not proof; an unknown value keeps its own name, an unreadable one is "unknown"
        if not isinstance(auth, str) or not AUTH_RAW.fullmatch(auth):
            out["presence"] = "unknown"
        else:
            out["presence"] = PRESENCE.get(auth, auth)
    if d.get("source_sha"):
        out["source_sha"] = ", ".join(s["sha"][:12] for s in d["source_sha"])
    return out


def _policy_text(policy: dict | None) -> tuple[str, str | None]:
    """The effective policy in words: who approves, and who is excluded (`not`)."""
    if not policy:
        return "", None
    who = policy.get("approvers") or []
    names = [w if isinstance(w, str) else str(w) for w in who]
    excluded = policy.get("not") or []
    return " or ".join(names), (" and ".join(str(x) for x in excluded) if excluded else None)


def _gate(gate: dict[str, Any] | None, ext: bool, decisions_source: list[dict] | None = None) -> dict:
    """A gate as the UI draws it. Without `read_ext/1` the host sends only state, gen and hash12."""
    gate = gate or {}
    state = gate.get("state", "n/a")
    decisions = gate.get("decisions") or []
    counting = [d for d in decisions if d["kind"] == "approved" and d["counting"] and not d["voided"]]
    voided = [d for d in decisions if d["kind"] == "approved" and d["voided"]]
    changes = [d for d in decisions if d["kind"] == "changes_requested" and d["counting"] and not d["voided"]]
    ui_state = "approved" if state == "approved" else "changes_requested" if changes and ext else "pending"
    approvers, excluded = _policy_text(gate.get("policy"))
    out = {
        "state": ui_state,
        "approvals": [_approval(d) for d in counting],
        # Unknown without read_ext/1: 0 and "" are the "not served" convention of the gate card (see `unavailable`).
        "needed": gate.get("count") or 0,
        "approvers": approvers,
        "hash": gate.get("hash12"),
        "required": gate.get("applies", state != "n/a"),
        "gen": gate.get("gen"),
    }
    if excluded:
        out["not"] = excluded
    if voided:
        out["voided"] = [_approval(d) for d in voided]
    if gate.get("waiting") is not None:
        out["waiting"] = gate["waiting"]
    if gate.get("blocked"):
        out["blocked"] = True
    # B11: what an approval still lacks (core's completeness rule); the page never offers Approve while it lacks any
    if isinstance(gate.get("missing"), list):
        out["missing"] = [str(m) for m in gate["missing"]]
    if gate.get("eligible") is not None:
        out["eligible"] = list(gate["eligible"])
    if decisions_source:
        out["source_sha"] = ", ".join(s["sha"][:12] for s in decisions_source)
    return out


def _verify(v) -> dict | None:
    """The task's exact `verify` command as the host gives it (a string). Untrusted text."""
    cmd = v.get("cmd") if isinstance(v, dict) else v
    return {"cmd": cmd} if isinstance(cmd, str) and cmd else None


def _attested(value) -> str:
    """Who vouches for a receipt: only the host's `agent` label is shown as such; anything else is "unknown"."""
    return "agent" if value == "agent" else "unknown"


def _manual_acceptance(row: dict) -> dict:
    out = {k: row[k] for k in ("proof", "accepted_stale") if k in row}
    if isinstance(row.get("accepted"), dict):
        accepted = row["accepted"]
        out["accepted"] = {k: accepted[k] for k in ("by", "at", "note", "auth") if k in accepted}
        at = out["accepted"].get("at")
        if hasattr(at, "isoformat"):
            out["accepted"]["at"] = at.isoformat()
    return out


def _task(t: dict, host: dict | None, ext: bool) -> dict:
    """A task as the UI draws it. "doing" is `leased_by` (v1.4 tasks have no started state)."""
    state = TASK_STATE.get(t["state"], "todo")
    host = host or {}
    leased = host.get("leased_by")
    if state == "todo" and leased:
        state = "doing"
    receipt = host.get("receipt")
    return {
        "id": t["id"],
        "text": t["text"],
        "verify": _verify(t.get("verify")),
        "proves": list(t["proves"]),
        "state": state,
        # The session only: which agent holds it and since when is not served.
        "lease": {"session": leased, "agent": "", "since": ""} if leased else None,
        "receipt": {
            "exit": receipt["exit"],
            "ms": receipt["ms"],
            **({"commit": receipt["commit"]} if receipt.get("commit") else {}),
            # H3b2: a client receipt is labelled; shown as the host stores it, never as host-verified. A receipt
            # the host did not label (older than H3b2) says so: "unknown", not "verified".
            "attested_by": _attested(receipt.get("attested_by")),
        }
        if receipt
        else None,
        "done_at": receipt["at"] if receipt and state == "done" else (host.get("at") if state == "done" else None),
    }


def _artifact(a: dict) -> dict:
    return {
        "name": a["name"],
        "kind": a["kind"] if a["kind"] in KINDS else "other",
        "bytes": a.get("bytes"),  # None: an addon artifact has no file
        "sha256": _hex(a.get("digest")),
        "ac": a.get("ac") or None,
        "task": a.get("task") or None,
        "label": a.get("label") or None,
        "added_by": _actor_name(a.get("by")),
        "added_by_kind": (a.get("by") or {}).get("kind")
        if (a.get("by") or {}).get("kind") in ("person", "agent", "host")
        else None,
        "at": a.get("at") or "",
        **({"addon": a["addon"], "ref": a["ref"]} if a.get("addon") else {}),
    }


def _verdict(v: dict | None) -> dict | None:
    if not v:
        return None
    sources = _sources(v.get("source_sha"))
    out = {
        "result": v["outcome"],
        "by": v["person"],
        "at": v["at"],
        "sources": sources,
        "current": v.get("current", True),
        "voided": v.get("voided", False),
    }
    if v.get("text") is not None:
        out["text"] = v["text"]
    if v.get("text_cut"):
        out["text_cut"] = True
    if len(sources) == 1:
        out["source_sha"] = sources[0]["sha"]
    return out


def document(
    data: dict, viewer: str | None = None, mine: dict[str, list[dict]] | None = None, ext: bool = False
) -> dict:
    """`ticket.get` as a TicketDocument. The F1 section 7 document carries the ticket.json fields, so no second read.

    `ext` is whether the host lists `read_ext/1`: without it the v1.4 fields are absent, i.e. unknown."""
    doc, log = data["doc"], data["log"]
    complete = not data.get("log_more", False) and bool(log)
    uid = doc["uid"]
    own = (mine or {}).get(uid, [])
    claim = data.get("claim") if ext else None
    row_claim = doc.get("claim")
    status = data.get("status") or doc["status"]
    full = {c["id"]: c for c in data.get("criteria", [])}
    host_tasks = {t["id"]: t for t in data.get("tasks") or []}
    asked = {q["id"]: q for q in data.get("questions", [])}
    answer_at = {e["seq"]: e["at"] for e in log}

    live = (claim or row_claim or {}).get("live")
    if own:
        turn = {"who": viewer, "why": _why(own[0])}
    elif not (claim or row_claim):
        turn = {"who": "nobody", "why": ""}
    elif live is False:
        turn = {"who": "nobody", "why": "The agent's claim has lapsed"}
    else:
        who = (claim or {}).get("agent") or (claim or row_claim)["session"]
        turn = {"who": "agent:" + who, "why": "An agent holds the claim"}

    questions, questions_state = [], []
    for q in doc["questions"]:
        info = asked.get(q["id"], {})
        entry = {
            "id": q["id"],
            "to": info.get("to_ref") or q["to"],
            "text": info.get("text") or q["text"],
            "blocking": q["blocking"],
        }
        if info.get("options"):
            entry["options"] = [{k: o[k] for k in ("key", "label", "cost") if k in o} for o in info["options"]]
        elif q.get("options"):
            entry["options"] = [{"key": k, "label": k} for k in q["options"]]
        for k in ("why", "recommended"):
            if info.get(k):
                entry[k] = info[k]
        questions.append(entry)
        state = info.get("state", "open")
        answer = info.get("answer")
        if answer is None and state == "answered" and info.get("answered_by"):
            answer = {"by": info["answered_by"], "at": answer_at.get(info.get("answered_seq"), "")}
        ui_answer = None
        if answer:
            ui_answer = {"by": answer["by"], "at": answer["at"]}
            if answer.get("option") is not None:
                ui_answer["option"] = answer["option"]
            if answer.get("text") is not None:
                ui_answer["text"] = answer["text"]
        asked_by = info.get("asked_by")
        questions_state.append(
            {
                **entry,
                # `stale`: the question changed after it was answered; it waits for a new answer.
                "state": "answered" if state == "answered" else "stale" if state == "stale" else "open",
                "asked_at": info.get("asked_at") or "",
                "asked_by": _actor_name(asked_by),
                "answer": ui_answer,
            }
        )
    tasks = [
        {"id": t["id"], "text": t["text"], "verify": _verify(t.get("verify")), "proves": list(t["proves"])}
        for t in doc["tasks"]
    ]
    acceptance = [{"id": a["id"], "text": a["text"]} for a in doc["acceptance"]]
    if ext and data.get("artifacts") is not None:
        artifacts = [_artifact(a) for a in data["artifacts"]]
    else:
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
                        "bytes": None,
                        "sha256": _hex(ev["digest"]),
                        "ac": crit["id"],
                        "added_by": ev["attested_by"],
                        "at": "",
                    }
                )
    visibility = doc.get("visibility", "workspace")
    people = doc.get("people") or {}
    verdict = _verdict(data.get("verdict")) if ext else None
    source = data.get("source") or []
    unavailable = list(NEVER_SERVED if ext else NOT_SERVED)
    if ext and data.get("artifacts") is None:
        unavailable.append("artifact manifest")
    if ext and data.get("artifacts_more"):
        unavailable.append("more artifacts than the first 1000")
    out = {
        "schema": "orch.ticket/2",
        "uid": uid,
        "key": doc["key"],
        "title": doc["title"],
        "type": doc["type"],
        "priority": doc.get("priority"),
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
        "status": _ui_status(status),
        "people": {
            "owner": doc.get("owner"),
            "assignees": list(people.get("assignees", [])),
            "reviewers": list(people.get("reviewers", [])),
            "watchers": list(people.get("watchers", [])),
        },
        "claim": {
            "agent": (claim or {}).get("agent") or "",
            "session": (claim or row_claim)["session"],
            "for": (claim or {}).get("for") or "",
            "grant": (claim or {}).get("grant") or "",
            "since": (claim or {}).get("since") or "",
            # `claim_ttl_min` has no read op yet (v1.5): the expiry is unknown, not "never".
            "expires": "",
            "live": (claim or row_claim).get("live"),
        }
        if (claim or row_claim)
        else None,
        "tasks_state": [_task(t, host_tasks.get(t["id"]), ext) for t in doc["tasks"]],
        "acceptance_state": [
            {
                **a,
                "state": "proven"
                if (
                    full.get(a["id"], {}).get("covered")
                    if full.get(a["id"], {}).get("covered") is not None
                    else bool(full.get(a["id"], {}).get("evidence"))
                )
                or src["evidence"]
                else "unproven",
                "evidence": _evidence(src["evidence"]),
                **_manual_acceptance(full.get(a["id"], {})),
            }
            for a, src in zip(acceptance, doc["acceptance"], strict=True)
        ],
        "questions_state": questions_state,
        "gates": {g: _gate(data["gates"].get(g), ext, _gate_sources(data["gates"].get(g))) for g in GATES},
        "artifacts": artifacts,
        "verdict": verdict,
        "turn": turn,
        "body": _sections(doc),
        "head": {"seq": log[-1]["seq"] if complete else 0, "hash": ""},
        "created_at": log[0]["at"] if log and log[0]["type"] == "ticket.created" else "",
        "updated_at": log[-1]["at"] if complete else "",
        "restricted": visibility != "workspace",
        "branch": {
            "name": next(iter((doc.get("links") or {}).get("branches", {}).values()), ""),
            "base": "",
            # The head and the diffstat come with the diff (Changes tab); `sources` lists what the host observed.
            "head": "",
            "commits": [],
            "files": 0,
            "additions": 0,
            "deletions": 0,
        },
        "unavailable": unavailable,
        "log_more": data.get("log_more", False),
        "read_ext": ext,
    }
    if status == "closed" or data.get("settled"):
        out["settled"] = True
    if data.get("closed"):
        out["closed"] = _closed(data["closed"])
    elif status == "closed":
        out["closed"] = {"resolution": "other", "duplicate_of": None, "at": "", "by": ""}
    if ext and data.get("sections"):
        out["section_edits"] = {
            sid: [
                {
                    "seq": r["seq"],
                    "at": r["at"],
                    "hash": r.get("hash"),
                    "by": _actor_name(r.get("actor")),
                    "via": r.get("via"),
                }
                for r in revs
            ]
            for sid, revs in data["sections"].items()
        }
    if ext and source:
        out["sources"] = [{"name": s["name"], "repo": s["repo"], "ref": s["ref"], "sha": s["sha"]} for s in source]
    return out


def _gate_sources(gate: dict | None) -> list[dict] | None:
    """The commit a standing verify or code approval signed: the source list of its latest counting decision."""
    for d in reversed((gate or {}).get("decisions") or []):
        if d.get("counting") and not d.get("voided") and d.get("source_sha"):
            return d["source_sha"]
    return None


KINDS = {"screenshot", "log", "report", "link", "dataset", "build", "diagram", "receipt", "feedback", "other"}


def _evidence(refs: list[str]) -> list[dict]:
    out = []
    for ref in refs:
        kind, _, name = ref.partition(":")
        if kind in ("artifact", "task") and name:
            out.append({"kind": kind, "ref": name})
    return out
