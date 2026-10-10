"""Questions (T6; ticket-format §5.4.1, §5.6): the last ``question.asked`` for an id defines it; the first valid
signed answer for the current question hash wins. Phone ``evidence`` answers are not in P1 (§5.3)."""

from __future__ import annotations

import copy
from typing import Any

from orch.canon import HashError, question_hash, question_id

from .codes import Code, Refusal
from .types import QuestionCore, TCore, WsCore


def role_holders(t: TCore, role: str) -> set[str]:
    return {t.owner} if role == "ticket_owner" else set(t.people[role])


def addressees(ws: WsCore, t: TCore, q: dict[str, Any]) -> list[str]:
    """Current members the question is for; owners and maintainers when ``to`` resolves to nobody (§5.4.1)."""
    to = q["to"]
    who = {to} if to.startswith("p_") else role_holders(t, to)
    who = {p for p in who if p in ws.members}
    if not who:
        who = {p for p, m in ws.members.items() if m.role in ("owner", "maintainer")}
    return sorted(who)


def may_answer(ws: WsCore, t: TCore, q: dict[str, Any], person: str) -> bool:
    m = ws.members.get(person)
    if m is None:
        return False
    if m.role in ("owner", "maintainer"):
        return True
    to = q["to"]
    return person == to if to.startswith("p_") else person in role_holders(t, to)


def asked(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    q = e["question"]
    try:
        qid = question_id(ws.workspace_id, t.uid, q["id"])
        h = question_hash(qid, t.uid, q["text"], q.get("options"))
    except HashError as err:
        return Refusal(Code.QUESTION_BAD_ID, str(err))
    if e["qid"] != qid:
        return Refusal(Code.QUESTION_BAD_ID, "qid is not derived from the question id (§5.6)")
    if e["hash"] != h:
        return Refusal(Code.QUESTION_BAD_HASH, "hash is not the question hash (§5.6)")
    a = e["actor"]
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
    person = e["actor"]["id"]
    if not may_answer(ws, t, cur.question, person):
        return Refusal(Code.ANSWER_NOT_ALLOWED, f"{person} may not answer {e['question']}")
    if "option" not in e and "text" not in e:
        return Refusal(Code.ANSWER_BAD_OPTION, "an answer needs an option or a text")
    if "option" in e and e["option"] not in {o["key"] for o in cur.question.get("options", [])}:
        return Refusal(Code.ANSWER_BAD_OPTION, f"{e['option']!r} is not an option")
    cur.answer = {"event": e["id"], "by": person, "option": e.get("option"), "text": e.get("text")}
    return None


def open_questions(t: TCore) -> list[QuestionCore]:
    return [q for _, q in sorted(t.questions.items(), key=lambda kv: int(kv[0][1:])) if q.answer is None]
