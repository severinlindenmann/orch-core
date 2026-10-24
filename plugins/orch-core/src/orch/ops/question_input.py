"""Turn what an author typed into a question (design #1123 section 3): the ONE parser of `orch ask` and batch `ask`.

Flags: ``--option 'key | title | text'`` (repeatable), ``--fact 'key | Label | value'`` (repeatable, ``key`` names an
option), ``--rec KEY``, ``--allow-other``, ``--details`` or ``--details-file``, ``--text-question``, or the whole
question as a strict JSON document (``--question-file``). Every limit is judged once, by
:func:`orch.canon.validate_question`; this module only reads the input, normalises the text (never mutating what the
validator then checks) and words the refusals in plain sentences.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from orch import canon
from orch.ops.errors import OrchError
from orch.ops.runtime import Call

#: the document keys of ``--question-file`` (``rec`` is the recommended option key, ``title`` an option's label)
DOC_KEYS = frozenset({"kind", "text", "why", "options", "rec", "allow_other", "details", "blocking", "to"})
_DOC_OPTION_KEYS = frozenset({"key", "title", "text", "facts", "cost"})
#: flags that shape the question and so cannot be combined with ``--question-file``
_SHAPING = ("text", "option", "fact", "rec", "allow_other", "details", "details_file", "text_question", "why", "to")
_FLAG = {
    "details_file": "--details-file",
    "text_question": "--text-question",
    "allow_other": "--allow-other",
    "non_blocking": "--non-blocking",
}
_MARKER = re.compile(r"(?:^|\s)\(?([A-Da-d])\)(?=\s|$)")


def _flag(name: str) -> str:
    return _FLAG.get(name) or ("--" + name.replace("_", "-") if name != "text" else "TEXT")


def _split(raw: str, flag: str, shape: str) -> list[str]:
    parts = [p.strip() for p in raw.split("|", 2)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise OrchError("invalid.input", f"{flag} {raw[:60]!r}: write it as {shape}")
    return parts


def _line(c: Call, value: Any, what: str) -> Any:
    """A one-line text, normalised; anything that is not a string is left for the validator to refuse."""
    return c.text(value, one_line=True, what=what) if isinstance(value, str) else value


def _from_flags(c: Call, args: Mapping[str, Any]) -> dict[str, Any]:
    q: dict[str, Any] = {"text": c.text(args["text"], what="question")}
    if args.get("text_question"):
        clash = [n for n in ("option", "fact", "rec") if args.get(n) is not None] + (
            ["allow_other"] if args.get("allow_other") else []
        )
        if clash:
            raise OrchError("invalid.input", f"--text-question takes no options: remove {_flag(clash[0])}")
        q["kind"] = "text"
    else:
        q["kind"] = "choice"
        options: dict[str, dict[str, Any]] = {}
        for raw in args.get("option") or []:
            parts = _split(raw, "--option", "'key | title' or 'key | title | text'")
            key = parts[0]
            if key in options:
                raise OrchError("invalid.input", f"two options have the key {key!r}")
            o: dict[str, Any] = {"key": key, "label": _line(c, parts[1], f"option {key} title")}
            if len(parts) == 3 and parts[2]:
                o["text"] = _line(c, parts[2], f"option {key} text")
            options[key] = o
        for raw in args.get("fact") or []:
            key, label, *rest = _split(raw, "--fact", "'key | Label | value'")
            value = rest[0] if rest else ""
            if not value:
                raise OrchError("invalid.input", f"--fact {raw[:60]!r}: write it as 'key | Label | value'")
            if key not in options:
                raise OrchError("invalid.input", f"--fact {raw[:60]!r}: {key!r} is not one of the --option keys")
            options[key].setdefault("facts", []).append(
                [_line(c, label, f"option {key} fact label"), _line(c, value, f"option {key} fact value")]
            )
        if not options:
            raise OrchError(
                "invalid.input", "give 2 to 6 --option 'key | title | text', or --text-question for an open question"
            )
        q["options"] = list(options.values())
        if args.get("rec") is not None:
            q["recommended"] = args["rec"]
        if args.get("allow_other"):
            q["allow_other"] = True
    if args.get("details") is not None and args.get("details_file") is not None:
        raise OrchError("invalid.input", "give exactly one of --details, --details-file")
    if args.get("details_file") is not None:
        q["details"] = c.text(c.read_file_text(args["details_file"]), what="details")
    elif args.get("details") is not None:
        q["details"] = c.text(args["details"], what="details")
    return q


def _from_doc(c: Call, args: Mapping[str, Any]) -> dict[str, Any]:
    for n in _SHAPING:
        if args.get(n) not in (None, False):
            raise OrchError("invalid.input", f"--question-file holds the whole question: remove {_flag(n)}")
    if args.get("non_blocking"):
        raise OrchError("invalid.input", '--question-file holds the whole question: put "blocking": false in it')
    try:
        doc = canon.loads_strict(c.read_file_text(args["question_file"]))
    except (canon.JcsError, ValueError):
        raise OrchError(
            "parse.text", "the question file is not strict JSON (no duplicate keys, no decimal numbers)"
        ) from None
    if type(doc) is not dict:
        raise OrchError("invalid.input", "the question file is one JSON object")
    extra = sorted(set(doc) - DOC_KEYS)
    if extra:
        raise OrchError("invalid.input", f"the question file has keys that orch does not know: {', '.join(extra)}")
    if "text" not in doc:
        raise OrchError("invalid.input", "the question file needs a text")
    q: dict[str, Any] = {}
    for k, v in doc.items():
        if k == "options":
            q["options"] = _doc_options(c, v)
        elif k == "rec":
            q["recommended"] = v
        elif k in ("blocking", "to", "allow_other"):
            q[k] = v
        else:
            q[k] = c.text(v, what=k) if isinstance(v, str) else v
    q.setdefault("kind", "choice" if "options" in q else "text")
    return q


def _doc_options(c: Call, v: Any) -> Any:
    if type(v) is not list:
        return v
    out = []
    for i, o in enumerate(v, 1):
        if type(o) is not dict:
            out.append(o)
            continue
        extra = sorted(set(o) - _DOC_OPTION_KEYS)
        if extra:
            raise OrchError("invalid.input", f"option {i} has keys that orch does not know: {', '.join(extra)}")
        built: dict[str, Any] = {}
        name = o["key"] if type(o.get("key")) is str else str(i)
        for k, x in o.items():
            if k == "title":
                built["label"] = _line(c, x, f"option {name} title")
            elif k == "facts":
                built["facts"] = (
                    [
                        [_line(c, f[0], f"option {name} fact label"), _line(c, f[1], f"option {name} fact value")]
                        if type(f) is list and len(f) == 2
                        else f
                        for f in x
                    ]
                    if type(x) is list
                    else x
                )
            else:
                built[k] = _line(c, x, f"option {name} {k}")
        out.append(built)
    return out


def marker_warning(q: Mapping[str, Any]) -> str | None:
    """Advice (never a refusal) when the question text lists its answers as ``a) b)`` instead of options."""
    markers = {m.lower() for m in _MARKER.findall(str(q.get("text", "")))}
    if len(markers) >= 2:
        return (
            "warning: the question text lists answers as a) b) ...; give them as --option 'key | title | text' "
            "so the person can pick one"
        )
    return None


def build_question(c: Call, args: Mapping[str, Any], qid_n: str) -> tuple[dict[str, Any], list[str]]:
    """The valid question ``qid_n`` the arguments describe, and its warnings. Raises ``invalid.input`` or
    ``parse.text`` in plain words."""
    if args.get("question_file") is not None:
        q = _from_doc(c, args)
    elif args.get("text") is None:
        raise OrchError("invalid.input", "give the question as TEXT or --question-file")
    else:
        q = _from_flags(c, args)
    to = q.pop("to", None) or args.get("to") or "ticket_owner"
    blocking = q.pop("blocking", None)
    if blocking is None:
        blocking = not args.get("non_blocking")
    if args.get("why") is not None:
        q["why"] = c.text(args["why"], what="why")
    q = {"id": qid_n, "to": to, "blocking": blocking, **q}
    try:
        canon.validate_question(q)
    except canon.QuestionError as e:
        raise OrchError("invalid.input", str(e)) from None
    warning = marker_warning(q)
    return q, [warning] if warning else []
