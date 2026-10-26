"""Decisions a person made on a ticket (F1 10.4 item 7) and the agent's **decision cursor**.

A decision is an answer, an approval, a change request, a verdict or an invalidation. ``wait`` hands them over one at
a time and is the only thing that moves the session's decision cursor (an explicit ``inbox`` hands over all and moves it
too); ``show`` lists the ones not handed over yet, with their content, so reading the ticket never loses an answer."""

from __future__ import annotations

from typing import Any

from orch.ops.runtime import Call, short

TYPES = frozenset({"question.answered", "gate.approved", "gate.changes_requested", "verdict.given", "gate.invalidated"})


def _by(e: dict[str, Any]) -> str:
    return e["actor"]["id"]


def decision(e: dict[str, Any], key: str) -> tuple[dict[str, Any], int] | None:
    """What a ticket event means for a waiting agent, as ``(fields, exit code)``; ``None`` when it is not a decision.
    The fields are exactly the ones F1 10.4 item 7 lists for the kind and nothing else."""
    t = e["type"]
    base = {"key": key, "seq": e["seq"]}
    if t == "question.answered":
        out = {"kind": "answered", **base, "question": e["question"], "by": _by(e)}
        for k in ("option", "text"):
            if e.get(k) is not None:
                out[k] = e[k]
        return out, 0
    if t == "gate.approved":
        return {"kind": "approved", **base, "gate": e["gate"], "by": _by(e)}, 0
    if t == "gate.changes_requested":
        return {"kind": "changes_requested", **base, "gate": e["gate"], "text": e["text"], "by": _by(e)}, 3
    if t == "verdict.given":
        out = {"kind": "verdict", **base, "outcome": e["outcome"], "by": _by(e)}
        if e["outcome"] == "fail":
            out["text"] = e["text"]
        return out, 3 if e["outcome"] == "fail" else 0
    if t == "gate.invalidated":
        return {"kind": "invalidated", **base, "gate": e["gate"]}, 3
    return None


def line(e: dict[str, Any], n: int = 120) -> str:
    """One undelivered decision for ``show`` and ``inbox`` (the content is ticket data: the caller fences it)."""
    got = decision(e, "")
    assert got is not None
    f = got[0]
    bits = [f"#{e['seq']}", f["kind"]]
    for k in ("question", "gate", "outcome"):
        if k in f:
            bits.append(f[k])
    if "option" in f:
        bits.append(f"option={f['option']}")
    text = f"{' '.join(bits)}: {short(f['text'], n)}" if "text" in f else " ".join(bits)
    return text


def undelivered(c: Call, view: Any, head: int | None = None) -> list[dict[str, Any]]:
    """The decisions on ``view`` after the session's decision cursor (none for a ticket it never touched)."""
    note = c.notes.get(view.uid)
    after = note["decided"]
    if after is None:
        return []
    return [e for e in c.store.events(view.uid, after=after) if e["type"] in TYPES]
