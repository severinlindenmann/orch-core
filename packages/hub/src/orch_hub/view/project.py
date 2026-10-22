"""A small projection of the logs the hub holds (workspace log, one ticket log) into exactly what the hub needs:
the member-list version, the workspace prefix and owner, the ticket's definition values, people, open questions.

This is NOT the F1 replay (protocol.md §10.7 "What the hub trusts"): it does not check who was allowed to sign what or
re-run authorization. It reads fields of events the host signed, and every value it derives is re-checked by the host
against its own log when the signed event arrives (§8.3 step 9). It also never reads body text or artifacts (not
synced in hv 0). Pure functions over decoded lines; any shape it cannot read is ``Refused("unavailable")``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..contract import suite as S
from ..contract.canon import CanonError, cj
from ..contract.hub import Refused

ROSTER_TYPES = ("workspace.created", "member.added", "member.removed", "role.changed")
TICKET_ROLES = ("ticket_owner", "assignees", "reviewers", "watchers")
_QUESTION = re.compile(r"Q[1-9][0-9]{0,5}")
_KEY = re.compile(r"([A-Z][A-Z0-9]{0,15})-([0-9]{1,9})")

#: ``ticket.json`` defaults for the seven paths a channel may ``set`` (ticket-format §3).
DEFAULTS: dict[str, Any] = {
    "ticket.title": None,
    "ticket.priority": "medium",
    "ticket.size": None,
    "ticket.labels": [],
    "ticket.due": None,
    "ticket.parent": None,
    "ticket.blocked_by": [],
}


def _bad() -> Refused:
    return Refused("unavailable")


def value_hash(value: Any) -> str:
    """``H("orch/v2/value|" || cj(value))`` (ticket-format §5.6): the ``base_rev`` of a ``ticket.json`` path."""
    try:
        return "sha256:" + S.sha256(b"orch/v2/value|" + cj(value)).hex()
    except CanonError:
        raise _bad() from None


def question_id(workspace_id: str, ticket_uid: str, question: str) -> str:
    """``qid``: first 16 bytes, 32 hex, of ``H("orch/v2/question-id|" || cj({workspace_id, ticket, question}))``."""
    body = {"workspace_id": workspace_id, "ticket": ticket_uid, "question": question}
    return S.sha256(b"orch/v2/question-id|" + cj(body))[:16].hex()


def question_hash(qid: str, ticket_uid: str, text: str, options: list[Any] | None) -> str:
    """``H("orch/v2/question|" || cj({question_id, ticket, text, options}))`` (``options`` ``[]`` when none)."""
    body = {"question_id": qid, "ticket": ticket_uid, "text": text, "options": options or []}
    try:
        return "sha256:" + S.sha256(b"orch/v2/question|" + cj(body)).hex()
    except CanonError:
        raise _bad() from None


@dataclass
class Workspace:
    roster_v: int = 0
    prefix: str | None = None
    owner_person: str | None = None
    next_number: int = 1  # filled by the caller from the ticket keys it knows


def project_workspace(lines: list[dict[str, Any]]) -> Workspace:
    w = Workspace()
    for e in lines:
        t = e.get("type")
        if t in ROSTER_TYPES:
            w.roster_v += 1
        if t == "workspace.created":
            p, o = e.get("prefix"), e.get("owner")
            if type(p) is str and type(o) is dict and type(o.get("person")) is str:
                w.prefix, w.owner_person = p, o["person"]
    if not lines:
        raise _bad()
    return w


@dataclass
class Question:
    id: str
    qid: str
    hash: str
    obj: dict[str, Any]
    answered: bool = False


@dataclass
class Ticket:
    uid: str
    key: str = ""
    title: str = ""
    ticket_type: str = ""
    closed: bool = False
    values: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))
    people: dict[str, list[str]] = field(default_factory=lambda: {r: [] for r in TICKET_ROLES})
    visibility: Any = "workspace"
    questions: dict[str, Question] = field(default_factory=dict)

    def next_question(self) -> str:
        n = max((int(q[1:]) for q in self.questions), default=0)
        return f"Q{n + 1}"


def project_ticket(lines: list[dict[str, Any]], workspace_id: str, uid: str) -> Ticket:
    """Replay of the ticket log's definition-level events. ``restore`` never appears in a view the hub accepted past a
    ``reset``, so it is not special here."""
    t = Ticket(uid)
    try:
        for e in lines:
            typ = e.get("type")
            if typ == "ticket.created":
                t.key, t.title, t.ticket_type = e["key"], e["title"], e["ticket_type"]
                t.values["ticket.title"] = e["title"]
                t.people["ticket_owner"] = [e["owner"]]
            elif typ == "ticket.updated":
                for path, v in (e.get("set") or {}).items():
                    if path in DEFAULTS:
                        t.values[path] = v
                if "ticket.title" in (e.get("set") or {}):
                    t.title = t.values["ticket.title"]
            elif typ == "ticket.closed":
                t.closed = True
            elif typ == "ticket.reopened":
                t.closed = False
            elif typ == "visibility.changed":
                t.visibility = e["visibility"]
            elif typ == "people.changed":
                role = "ticket_owner" if e["role"] == "owner" else e["role"]
                if role in t.people:
                    cur = [p for p in t.people[role] if p not in e["remove"]]
                    t.people[role] = sorted(set(cur) | set(e["add"])) if role != "ticket_owner" else (e["add"] or cur)
            elif typ == "question.asked":
                q = e["question"]
                qn = q["id"]
                if not _QUESTION.fullmatch(qn):
                    raise _bad()
                qid = question_id(workspace_id, uid, qn)
                h = question_hash(qid, uid, q["text"], q.get("options"))
                if e.get("qid") != qid or e.get("hash") != h:  # the host's own derived values must agree
                    raise _bad()
                t.questions[qn] = Question(qn, qid, h, q)
            elif typ == "question.answered":
                q2 = t.questions.get(e["question"])
                if q2 is not None and q2.hash == e.get("hash"):
                    q2.answered = True
    except KeyError, TypeError, AttributeError, ValueError:
        raise _bad() from None
    if not t.key:
        raise _bad()
    return t


def key_number(key: Any) -> tuple[str, int] | None:
    m = _KEY.fullmatch(key) if type(key) is str else None
    return (m[1], int(m[2])) if m else None
