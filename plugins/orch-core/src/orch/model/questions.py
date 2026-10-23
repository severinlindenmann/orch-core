"""Questions (T6; ticket-format §5.4.1, §5.6, §5.6.1): the last ``question.asked`` for an id defines it; the first
valid signed answer for the current question hash wins. Shape and answers follow the question's ``kind``
(``orch.canon.question``). Phone ``evidence`` answers are not in P1 (§5.3)."""

from __future__ import annotations

import copy
from typing import Any

from orch.canon import HashError, QuestionError, question_hash, question_id, validate_answer

from . import identity
from .codes import Code, Refusal
from .types import QuestionCore, TCore, WsCore


def role_holders(t: TCore, role: str) -> set[str]:
    return {t.owner} if role == "ticket_owner" else set(t.people[role])


def _isperson(ref: str) -> bool:
    return ref.startswith(("p_", "u_"))


def addressees(ws: WsCore, t: TCore, q: dict[str, Any]) -> list[str]:
    """Current members the question is for; owners and maintainers when ``to`` resolves to nobody (§5.4.1)."""
    to = q["to"]
    who = {to} if _isperson(to) else role_holders(t, to)
    who = {p for p in identity.canonical_set(ws, who) if p in ws.members}
    if not who:
        who = {p for p, m in ws.members.items() if m.role in ("owner", "maintainer")}
    return sorted(who)


def may_answer(ws: WsCore, t: TCore, q: dict[str, Any], person: str, role: Any = identity.LOOKUP) -> bool:
    """Canonical ids on both sides (D59, L2P4-C2); ``role`` is the signer's effective role (default ``role_of``)."""
    person = identity.canonical(ws, person)
    if role is identity.LOOKUP:
        role = identity.role_of(ws, person)
    if role is None:
        return False
    if role in ("owner", "maintainer"):
        return True
    to = q["to"]
    if _isperson(to):
        return person == identity.canonical(ws, to)
    return person in identity.canonical_set(ws, role_holders(t, to))


def asked(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    q = e["question"]
    try:
        qid = question_id(ws.workspace_id, t.uid, q["id"])
    except HashError as err:
        return Refusal(Code.QUESTION_BAD_ID, str(err))
    try:
        h = question_hash(qid, t.uid, q)  # validates the whole question (kind, limits, text rules), replay too
    except HashError as err:
        return Refusal(Code.QUESTION_BAD_HASH, str(err))
    if e["qid"] != qid:
        return Refusal(Code.QUESTION_BAD_ID, "qid is not derived from the question id (§5.6)")
    if e["hash"] != h:
        return Refusal(Code.QUESTION_BAD_HASH, "hash is not the question hash (§5.6)")
    a = e["actor"]
    if (r := identity.ref_refusal(ws, [q["to"]])) is not None:
        return r
    old = t.questions.get(q["id"])
    if old is not None and a["kind"] == "agent" and a.get("unattended") is True:
        return Refusal(Code.QUESTION_REASK, "an unattended session only asks new question ids")
    answer = old.answer if old is not None and old.hash == h else None
    t.questions[q["id"]] = QuestionCore(qid, h, copy.deepcopy(q), e["seq"], answer)
    qs = [x for x in t.fields["questions"] if x["id"] != q["id"]]
    t.fields["questions"] = sorted([*qs, copy.deepcopy(q)], key=lambda x: int(x["id"][1:]))
    return None


def answered(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    cur = t.questions.get(e["question"])
    if cur is None:
        return Refusal(Code.QUESTION_UNKNOWN, e["question"])
    if e["hash"] != cur.hash:
        return Refusal(Code.QUESTION_STALE, "the question changed since it was shown")
    if cur.answer is not None:
        return Refusal(Code.QUESTION_ANSWERED, "the first valid answer for this hash already won")
    sg = identity.signer(ws, e)
    if isinstance(sg, Refusal):
        return sg
    person = sg.person
    if not may_answer(ws, t, cur.question, person, sg.role):
        return Refusal(Code.ANSWER_NOT_ALLOWED, f"{person} may not answer {e['question']}")
    try:
        validate_answer(cur.question, e.get("option"), e.get("text"))  # per kind: Other needs allow_other, bounds
    except QuestionError as err:
        return Refusal(Code.ANSWER_BAD_OPTION, str(err))
    cur.answer = {"event": e["id"], "by": person, "option": e.get("option"), "text": e.get("text")}
    return None


def open_questions(t: TCore) -> list[QuestionCore]:
    return [q for _, q in sorted(t.questions.items(), key=lambda kv: int(kv[0][1:])) if q.answer is None]
