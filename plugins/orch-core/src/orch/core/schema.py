"""The ticket model as a versioned JSON document (addons spec §8.1). Generated from the core
constants, so a new status, section or task state shows up here without a second list to keep in sync.
The task list is the MC2-T view (docs/tasks-view.schema.json, format orch.tasks.v1), embedded as is.
Tools such as phone apps validate against `ticket_schema()` and refuse an unknown major `schema_version`."""
from __future__ import annotations

import copy
import json
import re
import tempfile
from functools import lru_cache
from pathlib import Path

from orch.core.model import Ticket

SCHEMA_VERSION = "1.8.0"  # 1.1: the Summary section. 1.2: type epic, sprint.
# 1.3: `verdict` {hash, round}: what a verdict must echo (orch.core.epics.verdict_hash)
# 1.4: `together` on an approve-requirements need: requirements and plan may be approved in one decision (F2);
# `move`: whose move it is, by the dashboard's rules (orch.dashboard.data.cards.move_summary)
# 1.5: `artifact_items`: what the ticket links (orch.core.artifacts.doc_items), names, labels and kinds only
# 1.6: `signed`: per approved gate and for a done verdict, whether this machine's signed ledger backs it, and who
# 1.7: `gates.verify.hash`: the verdict hash the verdict was given on (null when none was stored)
# 1.8: `artifact_items[].by` (who added it) and `.run` (a receipt's facts, orch task done --run); `revalidate`
TASKS_SCHEMA_FILE = "tasks-view.schema.json"
_PACKAGED = Path(__file__).resolve().parent.parent / "schemas" / TASKS_SCHEMA_FILE  # wheels: hatch force-include
_SOURCE = Path(__file__).resolve().parents[3] / "docs" / TASKS_SCHEMA_FILE  # plugin root: source checkout
_STAMP_KEYS = frozenset({"created", "updated", "asked", "answered", "approved", "at", "added", "plan_approved"})
_FIXED = "2026-10-02T09:00Z"
_HASH = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_S, _NS = {"type": "string"}, {"type": ["string", "null"]}
_SL = {"type": "array", "items": {"type": "string"}}
_META_KEYS = ("id", "title", "type", "priority", "size", "status", "created", "updated", "labels", "parent",
              "blocked_by", "follow_ups", "external", "repos", "branches", "prs")
_LIST_KEYS = ("labels", "blocked_by", "follow_ups", "external", "repos", "prs")


@lru_cache(maxsize=1)
def _tasks_view_schema_text() -> str:
    for path in (_PACKAGED, _SOURCE):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"{TASKS_SCHEMA_FILE} is missing from this orch install")


def tasks_view_schema() -> dict:
    """docs/tasks-view.schema.json (MC2-T), the one definition of the task list."""
    return json.loads(_tasks_view_schema_text())


def ticket_schema() -> dict:
    from orch.core.constants import PRIORITIES, SECTIONS, SIZES, STATUSES, TYPES
    from orch.core.query import NEEDS_ORDER
    from orch.core.questions import QTYPES
    from orch.core.tasks import FORMAT

    tasks_view = tasks_view_schema()
    option = {"type": "object", "additionalProperties": False, "required": ["key", "label", "cost"],
              "properties": {"key": _S, "label": _S, "cost": _NS}}
    question = {"type": "object", "required": ["id", "text", "why", "type", "options", "recommended", "blocking", "hash"],
                "properties": {"id": {"type": "string", "pattern": "^Q[1-9][0-9]*$"}, "text": _S, "why": _NS,
                               "type": {"enum": list(QTYPES)}, "options": {"type": "array", "items": option},
                               "recommended": {}, "blocking": {"type": "boolean"}, "asked": _NS, "answer": {},
                               "note": _NS, "answered": _NS, "via": _NS, "hash": _HASH}}
    changes = {"type": ["object", "null"], "properties": {"at": _NS, "message": _S, "hash": _HASH}}
    gate = {"type": "object", "required": ["state", "hash"],
            "properties": {"state": {"enum": ["pending", "approved", "invalidated"]}, "hash": _HASH,
                           "approved": _NS, "via": _NS, "changes_requested": changes,
                           # what `hash` binds, in order: section names, then frontmatter keys (hash v2)
                           "covers": _SL}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "orch:ticket:1",
        "title": "orch ticket",
        "type": "object",
        "required": ["schema_version", *_META_KEYS, "gates", "questions", "sections", "tasks", "needs", "claim", "artifacts",
                     "move", "signed"],
        "properties": {
            "schema_version": {"type": "string", "pattern": r"^1\.\d+\.\d+$"},
            "id": _S, "title": _S, "status": {"enum": list(STATUSES)}, "type": {"enum": list(TYPES)},
            "priority": {"enum": list(PRIORITIES)}, "size": {"enum": list(SIZES)},
            "created": _NS, "updated": _NS, "labels": _SL, "parent": _NS, "blocked_by": _SL, "follow_ups": _SL,
            "sprint": _NS,  # 1.2: a sprint id from the workspace config (planning only)
            "external": {"type": "array", "items": {"type": "object", "required": ["key"], "properties": {"key": _S, "url": _NS}}},
            "repos": {"type": "array"}, "branches": {"type": "object"}, "prs": {"type": "array"},
            "claim": {"type": "object", "properties": {"session": _NS, "harness": _NS, "at": _NS}},
            "gates": {"type": "object", "required": ["requirements", "plan", "verify"],
                      "properties": {"requirements": gate, "plan": gate,
                                     "verify": {"type": "object", "properties": {"verdict": _NS, "at": _NS, "via": _NS, "hash": _NS}}}},
            "questions": {"type": "array", "items": question},
            "sections": {"type": "object", "additionalProperties": _S, "properties": {name: _S for name in SECTIONS}},
            # The MC2-T task-list view, by reference to the embedded schema below (never a second definition).
            "tasks": {"$ref": tasks_view["$id"], "properties": {"format": {"const": FORMAT}}},
            # "round" on a "verdict" need is the testing round a phone's signed verdict must target (the event seq
            # of the latest move into testing); core refuses a verdict signed for an earlier round as stale.
            # 1.4: "together" (true, on approve-requirements only): the plan is drafted too and may be approved
            # with the requirements in one decision whose target carries both hashes (gates.*.hash).
            "needs": {"type": "array", "items": {"type": "object", "required": ["kind"],
                                                  "properties": {"kind": {"enum": list(NEEDS_ORDER)}, "detail": _S,
                                                                 "round": {"type": "integer", "minimum": 0},
                                                                 "together": {"type": "boolean"}}}},
            "artifacts": _SL,
            # 1.5: the ticket's linked artifacts: source file|link|static, kind, label; a file also its name and
            # sha256 (what an inline `artifact:<name>` reference shows), task and ac when given. Never a URL or path.
            "artifact_items": {"type": "array", "items": {
                "type": "object", "required": ["source", "kind", "label"],
                "properties": {"source": {"enum": ["file", "link", "static"]}, "kind": _S, "label": _S, "name": _S,
                               "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "task": _S,
                               "ac": {"type": "integer", "minimum": 1}, "by": _S,
                               # 1.8: a receipt's facts; step names and statuses, never the commands or the output
                               "run": {"type": "object", "properties": {
                                   "exit": {"type": ["integer", "null"]}, "timed_out": {"type": "boolean"},
                                   "commit": {"type": ["string", "null"]}, "dirty": {"type": "boolean"},
                                   "at": _S, "seconds": {"type": "integer", "minimum": 0}, "check": _NS,
                                   "repo": _NS,
                                   "steps": {"type": "array", "items": {"type": "object", "properties": {
                                       "name": _S, "status": {"enum": ["pass", "fail", "skip"]},
                                       "seconds": {"type": "integer", "minimum": 0}}}}}}}}},
            # 1.4: whose move it is (the dashboard's move chip): who you|agent|nobody, kind (approve-requirements,
            # approve-plan, re-approve, approve-epic, answer, task, verdict, repair, working, stale, blocked, ready,
            # done), label, ref (gate, question or task, or null); why (one line, when it is yours); epic (a
            # re-approval that is the epic's: a child whose gate the epic's charter no longer covers)
            "move": {"type": "object", "required": ["who", "kind", "label"],
                     "properties": {"who": {"enum": ["you", "agent", "nobody"]}, "kind": _S, "label": _S,
                                    "ref": _NS, "why": _S, "epic": _S}},
            # 1.3: what a verdict given elsewhere must send back: `hash` = verdict_hash (each ticket's id, status,
            # Acceptance criteria and Verification; for an epic over its open children) and the testing `round`
            # (null for an epic). Null while no verdict is due.
            "verdict": {"type": ["object", "null"], "required": ["hash", "round"],
                        "properties": {"hash": _HASH, "round": {"type": ["integer", "null"], "minimum": 0}}},
            # 1.6: R26 on the wire: for an approved gate (requirements, plan) and for a done ticket (verdict), whether
            # the signed ledger on this machine backs it (`signed`) and who (`by`: you, from your phone, by your epic charter, by delegation,
            # accepted, closed; null when not signed). A tampered, unknown or missing ledger entry is signed false.
            # A gate that is not approved, and a ticket that is not done, have no key. No key material, ever.
            # 1.8: an open or backlog ticket nobody touched for dashboard.revalidate_days (orch.core.query.idle_days)
            "revalidate": {"type": ["object", "null"], "additionalProperties": False, "required": ["idle_days"],
                           "properties": {"idle_days": {"type": "integer", "minimum": 1}}},
            "signed": {"type": "object", "properties": {k: {"type": "object", "required": ["signed", "by"],
                       "properties": {"signed": {"type": "boolean"}, "by": _NS}} for k in ("requirements", "plan", "verdict")}},
        },
        "$defs": {"tasks_view": tasks_view},
    }


def _gate(ticket, gate: str) -> dict:
    from orch.core.gates import gate_covers, gate_hash, gate_state
    g = (ticket.meta.get("gates") or {}).get(gate) or {}
    cr = g.get("changes_requested")
    return {"state": gate_state(ticket, gate), "hash": gate_hash(ticket, gate), "covers": gate_covers(gate, ticket),
            "approved": g.get("approved"),
            "via": g.get("via"),
            "changes_requested": {"at": cr.get("at"), "message": str(cr.get("message") or ""), "hash": cr.get("hash")}
            if isinstance(cr, dict) and cr.get("hash") else None}


def ticket_document(ws, ticket, *, entries=None) -> dict:
    from orch.core import query, tasks_view
    from orch.core.constants import SECTIONS
    from orch.core.questions import question_hash

    m = ticket.meta
    doc = {"schema_version": SCHEMA_VERSION}
    for key in _META_KEYS:
        value = m.get(key)
        doc[key] = (copy.deepcopy(value) if value is not None
                    else [] if key in _LIST_KEYS else {} if key == "branches" else None)
    claim = m.get("claim") if isinstance(m.get("claim"), dict) else {}
    doc["sprint"] = str(m["sprint"]) if isinstance(m.get("sprint"), (str, int)) and str(m["sprint"]) else None
    doc["claim"] = {"session": claim.get("session"), "harness": claim.get("harness"), "at": claim.get("at")}
    verify = (m.get("gates") or {}).get("verify") or {}
    doc["gates"] = {"requirements": _gate(ticket, "requirements"), "plan": _gate(ticket, "plan"),
                    "verify": {"verdict": verify.get("verdict"), "at": verify.get("at"), "via": verify.get("via"),
                               "hash": verify.get("hash")}}
    doc["questions"] = [{**copy.deepcopy(q), "hash": question_hash(q)} for q in m.get("questions") or [] if isinstance(q, dict)]
    names = list(SECTIONS) + [n for n in ticket.sections if n not in SECTIONS]
    doc["sections"] = {n: ticket.section(n) for n in names}
    doc["tasks"] = tasks_view.view(ws, ticket, entries)
    doc["needs"] = [{"kind": i["kind"], "detail": i.get("detail", ""), **({"round": i["round"]} if "round" in i else {}),
                     **({"together": True} if i.get("together") else {})}
                    for i in query.needs_you(ws, entries=entries) if str(i.get("ticket", "")).upper() == ticket.id.upper()]
    doc["artifacts"] = [p.relative_to(ws.artifacts_dir / ticket.id).as_posix() for p in query.artifact_list(ws, ticket.id)]
    from orch.core.artifacts import doc_items
    doc["artifact_items"] = doc_items(ticket)
    doc["verdict"] = verdict_target(ws, ticket, entries=entries)
    from orch.dashboard.data.cards import move_summary, ticket_card
    doc["move"] = move_summary(ticket_card(ws, ticket, entries=entries))
    doc["signed"] = _signed(ws, ticket)
    idle = query.idle_days(ws, m, str(m.get("status") or ""))
    doc["revalidate"] = {"idle_days": idle} if idle else None  # 1.8: freshness, derived from `updated`
    return doc


def _signed(ws, ticket) -> dict:
    """1.6 `signed`: the same checks as the dashboard's chips (story.gate_signers, story.done_signer); only a
    verified human entry or a signed charter is `signed`, a delegation or anything the ledger does not back is not.
    One ledger read serves both checks."""
    from orch.core import ledger
    from orch.core.events import read_events
    from orch.dashboard.data import story
    signed = ledger.entries(ws)
    out = {g: {"signed": who != "by delegation", "by": who} for g, who in story.gate_signers(ws, ticket, signed).items()}
    for g, v in out.items():
        if v["by"] is None:
            v["signed"] = False
    if ticket.status == "done":
        who = story.done_signer(ws, ticket, read_events(ws, ticket.id), signed)
        out["verdict"] = {"signed": who is not None, "by": who}
    return out


def verdict_target(ws, ticket, *, entries=None) -> dict | None:
    """{hash, round} a verdict on `ticket` must echo, or None while none is due: a ticket in testing (round: the
    latest testing round), or an open epic whose open children are all in testing (round: None)."""
    from orch.core import epics
    from orch.core.events import latest_testing_round, read_events
    if epics.is_epic(ticket):
        kids = epics.open_children(ws, ticket, entries) if ticket.status == "open" else []
        if not kids or any(k.status != "testing" for k in kids):
            return None
        return {"hash": epics.verdict_hash(kids, ws), "round": None}
    if ticket.status != "testing":
        return None
    return {"hash": epics.verdict_hash([ticket], ws), "round": latest_testing_round(read_events(ws, ticket.id))}


def ticket_from_document(doc: dict) -> Ticket:
    """The persisted part of a document as a Ticket (derived keys such as hashes, needs and tasks are recomputed
    from it). Used by tools that import tickets and by the round-trip test."""
    from orch.core.model import new_ticket

    t = new_ticket(doc["id"], doc["title"], type=doc["type"], priority=doc["priority"], size=doc["size"],
                   created=doc.get("created") or _FIXED)
    for key in _META_KEYS:
        t.meta[key] = copy.deepcopy(doc.get(key))
    t.meta["claim"] = dict(doc.get("claim") or {})
    if doc.get("sprint"):
        t.meta["sprint"] = doc["sprint"]
    gates = doc.get("gates") or {}
    for gate in ("requirements", "plan"):
        g = gates.get(gate) or {}
        # The document carries the gate's current hash; an invalidated gate keeps no matching hash.
        approved_hash = g.get("hash") if g.get("approved") and g.get("state") != "invalidated" else None
        t.meta["gates"][gate] = {"approved": g.get("approved"), "via": g.get("via"), "hash": approved_hash}
        if g.get("changes_requested"):
            t.meta["gates"][gate]["changes_requested"] = {**g["changes_requested"], "by": "you"}
    t.meta["gates"]["verify"] = dict(gates.get("verify") or {})
    t.meta["questions"] = [{k: v for k, v in q.items() if k != "hash"} for q in doc.get("questions") or []]
    for name, text in (doc.get("sections") or {}).items():
        t.set_section(name, text)
    return t


def _fix_stamps(value):
    if isinstance(value, dict):
        return {k: (_FIXED if k in _STAMP_KEYS and isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}T", v)
                    else _fix_stamps(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_fix_stamps(v) for v in value]
    return value


_EXAMPLE_TASKS = ("- [x] T1 Inventory the meter export jobs\n  - ref: ac:1\n  - note: 14 jobs listed\n"
                  "- [/] T2 Write the CSV exporter\n  - needs: T1\n  - verify: pytest tests/test_export.py\n"
                  "- [ ] T3 Ask the client for the delimiter\n  - owner: human")


def example_document() -> dict:
    """A fixed example ticket in the current format (stamps normalised), for docs and tool tests."""
    from orch.core import store
    from orch.core.gates import gate_hash
    from orch.core.model import new_ticket
    from orch.core.questions import build_questions
    from orch.testing.workspace import fake_workspace

    with tempfile.TemporaryDirectory() as tmp:
        fw = fake_workspace(Path(tmp), customer="Acme Energy", prefix="DEMO")
        t = new_ticket("DEMO-0038", "Export the meter readings as CSV", type="feature", priority="high", size="m",
                       created=_FIXED)
        t.meta["status"] = "waiting"
        t.meta["labels"] = ["export"]
        for name, text in {"Ask": "The client needs the readings as CSV.",
                           "Requirements": "- One file per day", "Acceptance criteria": "- [ ] Opens in Excel",
                           "Plan": "1. Inventory\n2. Exporter", "Tasks": _EXAMPLE_TASKS}.items():
            t.set_section(name, text)
        t.meta["gates"]["requirements"] = {"approved": _FIXED, "via": "dashboard", "hash": gate_hash(t, "requirements")}
        t.meta["gates"]["plan"] = {"approved": _FIXED, "via": "dashboard", "hash": gate_hash(t, "plan")}
        t.meta["questions"] = build_questions(
            [{"text": "Which timestamp format?", "why": "Excel parses only some formats",
              "options": [{"label": "ISO 8601", "cost": "none"}, {"label": "Local time"}], "recommended": "A"}],
            [], _FIXED)
        t.meta["claim"] = {"session": "7f3c9a21", "harness": "claude-code", "at": _FIXED}
        # 1.8: a receipt of a failing `orch task done --run` (orch.core.receipts), added by the agent
        t.meta["artifacts"] = [{
            "name": "receipt-T2-20261002T090000Z.log", "kind": "receipt", "sha256": "5" * 64, "size": 812,
            "added": _FIXED, "by": "agent:claude-code:7f3c9a21", "task": "T2",
            "label": "T2 verify: test failed with exit 1 at 1a2b3c4",
            "run": {"exit": 1, "timed_out": False, "commit": "1a2b3c4" + "0" * 33, "dirty": False,
                    "at": "2026-10-02T09:00:00Z", "seconds": 42, "check": "verify",
                    "steps": [{"name": "build", "run": "npm run build", "status": "pass", "exit": 0,
                               "timed_out": False, "seconds": 30},
                              {"name": "test", "run": "pytest tests/test_export.py", "status": "fail", "exit": 1,
                               "timed_out": False, "seconds": 12}]}}]
        store.save(fw.ws, t)
        return _fix_stamps(ticket_document(fw.ws, t))
