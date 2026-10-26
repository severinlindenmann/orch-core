"""A small projection of the logs the hub holds (workspace log, one ticket log) into exactly what the hub needs:
the member-list version, the workspace prefix and owner, the ticket's definition values, people, open questions.

This is NOT the F1 replay (protocol.md §10.7 "What the hub trusts"): it does not check who was allowed to sign what or
re-run authorization. It reads fields of events the host signed, and every value it derives is re-checked by the host
against its own log when the signed event arrives (§8.3 step 9). It also never reads body text or artifacts (not
synced in hv 0). Pure functions over decoded lines; any shape it cannot read is ``Refused("unavailable")``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contract import phase4 as P4
from ..contract import suite as S
from ..contract.canon import CanonError, cj
from ..contract.hub import Refused

ROSTER_TYPES = ("workspace.created", "member.added", "member.removed", "role.changed")
TICKET_ROLES = ("ticket_owner", "assignees", "reviewers", "watchers")
_QUESTION = re.compile(r"Q[1-9][0-9]{0,5}")
_NO_HASH = "sha256:" + "00" * 32
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


#: The question keys outside ``spec`` (ticket-format §5.6.1): routing and the two parts hashed on their own.
_NOT_SPEC = ("id", "to", "blocking", "text", "options")
#: What this hub can render and verify (design #1123 rev 4 §2). Any other kind, spec key or option key makes that ONE
#: question "unverifiable": shown, never answerable, the ticket stays.
KINDS = ("choice", "text")
SPEC_KEYS = frozenset({"kind", "why", "details", "recommended", "allow_other"})
OPTION_KEYS = frozenset({"key", "label", "text", "facts", "cost"})


def question_spec(question: Mapping[str, Any]) -> dict[str, Any]:
    """``spec``: every key of the raw question except ``id``, ``to``, ``blocking``, ``text``, ``options``."""
    return {k: v for k, v in question.items() if k not in _NOT_SPEC}


def question_hash(qid: str, ticket_uid: str, question: Mapping[str, Any]) -> str:
    """``H("orch/v2/question|" || cj({question_id, ticket, text, options, spec}))`` over the RAW question: ``options``
    is ``[]`` when absent, ``spec`` is :func:`question_spec`. The one hash entry (core ``canon.question_hash``); it
    hashes what it is given and checks no limits (:func:`renderable` is the reader's check)."""
    try:
        body = {
            "question_id": qid,
            "ticket": ticket_uid,
            "text": question["text"],
            "options": question.get("options") or [],
            "spec": question_spec(question),
        }
        return "sha256:" + S.sha256(b"orch/v2/question|" + cj(body)).hex()
    except CanonError, KeyError, TypeError:
        raise _bad() from None


# Unicode White_Space (PropList, 25 code points), pinned like core's WHITE_SPACE.
_WHITE_SPACE = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def _field(x: Any, n: int, *, one_line: bool = False) -> bool:
    """One text field under the cheap core limits (core ``check_plain``): 1..``n`` code points and 4096 bytes, NFC, not blank
    (Unicode White_Space), no character ``render_text`` would escape, and no LF or tab in a one-line field. Exact fold tables
    stay core's (the host re-checks on admission). Also core L2 (no leading/trailing White_Space in a one-line field) and L3
    (no combining mark, category M*, at the start of any line; the runtime's Unicode tables stand in for core's 16.0 ones)."""
    if type(x) is not str or not x or len(x) > n or not x.strip():
        return False
    try:
        if len(x.encode("utf-8")) > 4096:
            return False
    except UnicodeEncodeError:
        return False
    if unicodedata.normalize("NFC", x) != x or (one_line and ("\n" in x or "\t" in x)):
        return False
    if one_line and (x[0] in _WHITE_SPACE or x[-1] in _WHITE_SPACE):  # core L2: leading/trailing White_Space
        return False
    if any(
        ln and unicodedata.category(ln[0]).startswith("M") for ln in x.split("\n")
    ):  # core L3: mark at any line start
        return False
    flat = x.replace("\t", " ")
    return P4.render_text(flat) == flat


def _fold(s: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", s).casefold())


def _option_ok(o: Any) -> bool:
    if (
        type(o) is not dict
        or not set(o) <= OPTION_KEYS
        or not (_field(o.get("key"), 32) and _field(o.get("label"), 60, one_line=True))
    ):
        return False
    if not re.fullmatch(r"[a-z][a-z0-9_]*", o["key"]):
        return False
    if ("text" in o and not _field(o["text"], 200, one_line=True)) or (
        "cost" in o and not _field(o["cost"], 60, one_line=True)
    ):
        return False
    f = o.get("facts")
    if f is None:
        return True
    if not (type(f) is list and 1 <= len(f) <= 3):
        return False
    if not all(
        type(p) is list and len(p) == 2 and _field(p[0], 24, one_line=True) and _field(p[1], 40, one_line=True)
        for p in f
    ):
        return False
    return len({_fold(p[0]) for p in f}) == len(f)


def renderable(q: Any) -> bool:
    """Whether this hub knows every part of the question well enough to show it and check its hash, and the cheap core
    limits hold (see :func:`_field`; unique titles under ``casefold(NFC(.))``): known kind, spec and option keys,
    ``[label, value]`` facts, a boolean ``blocking``. A question that fails is "unverifiable", never answerable."""
    if not isinstance(q, Mapping) or type(q.get("blocking")) is not bool:
        return False
    spec, opts = question_spec(q), q.get("options")
    kind = spec.get("kind")
    if kind not in KINDS or not set(spec) <= SPEC_KEYS or not _field(q.get("text"), 140):
        return False
    if ("why" in spec and not _field(spec["why"], 4096)) or ("details" in spec and not _field(spec["details"], 1500)):
        return False
    if "allow_other" in spec and spec["allow_other"] is not True:
        return False
    if kind == "text":
        return opts is None and "recommended" not in spec and "allow_other" not in spec
    if type(opts) is not list or not 2 <= len(opts) <= 6 or not all(_option_ok(o) for o in opts):
        return False
    keys = [o["key"] for o in opts]
    return (
        len(set(keys)) == len(keys)
        and len({_fold(o["label"]) for o in opts}) == len(opts)
        and ("recommended" not in spec or spec["recommended"] in keys)
    )


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
    verified: bool = True  # the hash recomputes from the raw question AND this hub can render every part of it


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
                try:
                    h: Any = question_hash(qid, uid, q)
                except Refused:
                    h = None
                claimed = e.get("hash")
                # a question that fails to verify stays in the ticket, marked: never shown as answerable, never drops it
                ok = h is not None and e.get("qid") == qid and claimed == h and renderable(q)
                t.questions[qn] = Question(qn, qid, claimed if P4._hash(claimed) else _NO_HASH, q, verified=ok)
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


def identities(w: Workspace, user_id: str, owner_alias: str | None) -> set[str]:
    """The person ids this hub user answers to: their ``u_`` id, plus the workspace owner's person id when the hub
    aliased the owner to this user (registry owner alias)."""
    me = {"u_" + user_id}
    if w.owner_person and owner_alias == user_id:
        me.add(w.owner_person)
    return me


def visible_to(t: Ticket, me: set[str]) -> bool:
    """The ONE visibility rule (needs-you AND build), core's ``can_see`` without membership: protocol.md §10.7
    "Visibility", vectors ``visible_*``. The ticket owner has no special standing."""
    return P4.visible(t.visibility, me)
