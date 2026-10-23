"""The question shape, its kinds and its answers (ticket-format §5.6.1, design #1123 rev 4).

One shape, no legacy branch. Every question has a ``kind`` from the closed registry :data:`KINDS`:

* ``choice``: 2..6 options ``{key, label, text?, facts?, cost?}``, an optional ``recommended`` key and an optional
  ``allow_other: true``. The answer is an ``option`` (with an optional ``text`` note that never replaces it), or a
  ``text`` alone ("something else"), legal only when ``allow_other`` is bound.
* ``text``: an open question, no options. The answer is a ``text``.

Common keys: ``id``, ``to``, ``text`` (the question, 1..140 code points, may span lines), ``blocking``, ``kind`` and
the optional ``why`` and ``details`` (plain, multi-line). Absent is the only "none": ``""``, ``[]``, ``null`` and
``allow_other: false`` are refused, so one rendering has one encoding and one hash.

:func:`validate_question` is the one validator. :func:`orch.canon.question_hash` calls it, so nothing hashes, signs
or replays a question that breaks it; replay runs it too, so these limits are frozen for kind ``choice`` v1 and a
later tightening must be admit-only. Every limit counts code points (Python ``len``, JS ``Array.from(s).length``,
Swift ``unicodeScalars.count``) and every string is also at most 4096 UTF-8 bytes. Text is checked with
:func:`orch.canon.text.check_plain` (verify, never normalise). "The same title" and "the same fact label" compare
:func:`orch.canon.text.fold`.

:func:`question_spec` is the hash-bound object beside ``text`` and ``options``: every question key except
:data:`ROUTING` and ``text``/``options``. Reads carry it verbatim so a client hashes
``{question_id, ticket, text, options, spec}`` exactly as given and never rebuilds it from view keys.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .text import TextError, check_plain, fold

__all__ = [
    "ANSWER_MAX",
    "KINDS",
    "LIMITS",
    "ROUTING",
    "QuestionError",
    "question_spec",
    "validate_answer",
    "validate_question",
]


class QuestionError(ValueError):
    """The question or the answer breaks the shape of ticket-format §5.6.1."""


ROUTING = frozenset({"id", "to", "blocking"})
"""Question keys that are routing, not content: outside the question hash (who it is for, whether work waits)."""

LIMITS: dict[str, tuple[int, int]] = {
    "question": (1, 140),
    "options": (2, 6),
    "key": (1, 32),
    "label": (1, 60),
    "option_text": (1, 200),
    "cost": (1, 60),
    "facts": (1, 3),
    "fact_label": (1, 24),
    "fact_value": (1, 40),
    "details": (1, 1500),
    "why": (1, 4096),
}
"""(min, max) in code points or items; every string is also at most 4096 UTF-8 bytes."""

ANSWER_MAX = 1000
"""An answer's ``text`` (Other, a note, or the answer to a ``text`` question): 1..1000 code points, 4096 bytes."""

_QID = re.compile(r"Q[1-9][0-9]*")
_TOKEN = re.compile(r"[a-z][a-z0-9_]*")
_COMMON = frozenset({"id", "to", "text", "blocking", "kind", "why", "details"})
_OPTION_KEYS = frozenset({"key", "label", "text", "facts", "cost"})


def _plain(v: object, what: str, *, one_line: bool, limit: str) -> str:
    try:
        return check_plain(v, one_line=one_line, limit=LIMITS[limit][1], what=what)
    except TextError as e:
        raise QuestionError(str(e)) from None


def _keys(obj: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str], what: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise QuestionError(f"{what} has keys this kind does not define: {', '.join(map(str, extra))}")
    missing = sorted(required - set(obj))
    if missing:
        raise QuestionError(f"{what} is missing {', '.join(missing)}")


def _option(o: object, i: int) -> str:
    if type(o) is not dict:
        raise QuestionError(f"option {i + 1} must be an object")
    _keys(o, _OPTION_KEYS, frozenset({"key", "label"}), f"option {i + 1}")
    key = o["key"]
    if type(key) is not str or not _TOKEN.fullmatch(key) or len(key) > LIMITS["key"][1]:
        raise QuestionError(f"option {i + 1}: the key must be a-z, 0-9 or _, start with a letter, at most 32 long")
    _plain(o["label"], f"option {key} title", one_line=True, limit="label")
    if "text" in o:
        _plain(o["text"], f"option {key} text", one_line=True, limit="option_text")
    if "cost" in o:
        _plain(o["cost"], f"option {key} cost", one_line=True, limit="cost")
    if "facts" in o:
        facts = o["facts"]
        lo, hi = LIMITS["facts"]
        if type(facts) is not list or not lo <= len(facts) <= hi:
            raise QuestionError(f"option {key}: facts must be a list of {lo} to {hi} [label, value] pairs")
        seen: set[str] = set()
        for f in facts:
            if type(f) is not list or len(f) != 2:
                raise QuestionError(f"option {key}: each fact is exactly [label, value]")
            label = _plain(f[0], f"option {key} fact label", one_line=True, limit="fact_label")
            _plain(f[1], f"option {key} fact value", one_line=True, limit="fact_value")
            if fold(label) in seen:
                raise QuestionError(f"option {key}: two facts have the same label {label!r}")
            seen.add(fold(label))
    return key


def _choice(q: Mapping[str, Any]) -> None:
    _keys(q, _COMMON | {"options", "recommended", "allow_other"}, frozenset({"options"}), "a choice question")
    opts = q["options"]
    lo, hi = LIMITS["options"]
    if type(opts) is not list or not lo <= len(opts) <= hi:
        raise QuestionError(f"a choice question has {lo} to {hi} options")
    keys = [_option(o, i) for i, o in enumerate(opts)]
    if len(set(keys)) != len(keys):
        raise QuestionError("two options have the same key")
    titles = [fold(o["label"]) for o in opts]
    if len(set(titles)) != len(titles):
        raise QuestionError("two options have the same title (ignoring case)")
    if "recommended" in q and q["recommended"] not in keys:
        raise QuestionError("recommended must be one of the option keys")
    if "allow_other" in q and q["allow_other"] is not True:
        raise QuestionError("allow_other is true or absent")


def _text_kind(q: Mapping[str, Any]) -> None:
    _keys(q, _COMMON, frozenset(), "a text question")


def _answer_text(text: object) -> None:
    try:
        check_plain(text, one_line=False, limit=ANSWER_MAX, what="the answer text")
    except TextError as e:
        raise QuestionError(str(e)) from None


def _choice_answer(q: Mapping[str, Any], option: object, text: object) -> None:
    if option is None and text is None:
        raise QuestionError("an answer needs an option or a text")
    if text is not None:
        _answer_text(text)
    if option is None:
        if q.get("allow_other") is not True:
            raise QuestionError("this question takes one of its options, not a text of your own")
        return
    if option not in {o["key"] for o in q["options"]}:
        raise QuestionError(f"{option!r} is not an option")


def _text_answer(q: Mapping[str, Any], option: object, text: object) -> None:
    if option is not None:
        raise QuestionError("a text question has no options")
    if text is None:
        raise QuestionError("an answer to a text question needs a text")
    _answer_text(text)


@dataclass(frozen=True, slots=True)
class Kind:
    """One registry entry: the kind's own question keys and its answer rule. The signed review renderer of each kind
    lives in ``orch.host.signcontent``."""

    validate: Callable[[Mapping[str, Any]], None]
    answer: Callable[[Mapping[str, Any], object, object], None]


KINDS: dict[str, Kind] = {"choice": Kind(_choice, _choice_answer), "text": Kind(_text_kind, _text_answer)}
"""The closed kind registry. A new kind is a new contract decision (design #1123 §9), not just an entry here."""


def validate_question(q: object) -> Mapping[str, Any]:
    """Refuse (:class:`QuestionError`) anything but a whole, valid question of a known kind. Returns ``q``."""
    if type(q) is not dict:
        raise QuestionError("a question must be an object")
    kind = q.get("kind")
    if type(kind) is not str or kind not in KINDS:
        raise QuestionError(f"unknown question kind {kind!r} (known: {', '.join(sorted(KINDS))})")
    for k in ("id", "to", "text", "blocking"):
        if k not in q:
            raise QuestionError(f"a question is missing {k}")
    if type(q["id"]) is not str or not _QID.fullmatch(q["id"]):
        raise QuestionError("a question id is Q1, Q2, ...")
    if type(q["to"]) is not str or not q["to"]:
        raise QuestionError("to must name a person or a role")
    if type(q["blocking"]) is not bool:
        raise QuestionError("blocking is true or false")
    _plain(q["text"], "the question", one_line=False, limit="question")
    if "why" in q:
        _plain(q["why"], "why", one_line=False, limit="why")
    if "details" in q:
        _plain(q["details"], "details", one_line=False, limit="details")
    KINDS[kind].validate(q)
    return q


def question_spec(q: Mapping[str, Any]) -> dict[str, Any]:
    """The hash-bound keys beside ``text`` and ``options``: everything except :data:`ROUTING`, ``text``, ``options``.
    Always holds ``kind``. Call on a validated raw question, never on a read view."""
    return {k: v for k, v in q.items() if k not in ROUTING and k not in ("text", "options")}


def validate_answer(q: Mapping[str, Any], option: object = None, text: object = None) -> None:
    """Refuse (:class:`QuestionError`) an answer the question's kind does not take. ``None`` means absent."""
    validate_question(q)
    KINDS[q["kind"]].answer(q, option, text)
