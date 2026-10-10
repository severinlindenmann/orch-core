"""Small builders for the declaration modules, so each module reads as the declaration and nothing else.

Input schemas use three annotations the parser generator reads (JSON Schema allows unknown keywords):
``x-positional`` (top level, the names that are positional, in order), ``x-short`` (a one-letter flag) and
``x-metavar`` (the placeholder in usage). Everything else is plain JSON Schema.
"""

from __future__ import annotations

from typing import Any

from orch.ops.base import Operation, cli_words
from orch.ops.errors import ERRORS, GLOBAL_ERRORS
from orch.ops.preconditions import PRECONDITIONS

REF_PATTERN = r"^(?:(?:[A-Z][A-Z0-9]{0,15}-)?[0-9]+(?:/T[1-9][0-9]*)?|T[1-9][0-9]*)$"
TASK_REF_PATTERN = r"^(?:(?:[A-Z][A-Z0-9]{0,15}-)?[0-9]+/)?T[1-9][0-9]*$"
NO_FLAGS: dict[str, Any] = {}


def S(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **kw}


def I(desc: str, **kw: Any) -> dict[str, Any]:  # noqa: E743
    return {"type": "integer", "description": desc, **kw}


def B(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "boolean", "description": desc, **kw}


SECTIONS = [
    "summary",
    "context",
    "requirements",
    "out_of_scope",
    "plan",
    "decisions",
    "verification",
    "findings",
    "current_state",
]
LABEL_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,31}$"
TOKEN_PATTERN = r"^[a-z][a-z0-9_]*$"
AC_PATTERN = r"^AC[1-9][0-9]*$"
# Fields a ticket may set through `orch set`; person-only ones (visibility, owner, people) have their own operations.
SET_PATTERN = r"^(?:title|priority|size|labels|due|links|parent|blocked_by)=."


def L(desc: str, *, split: bool = False, items: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    """A list of strings. ``--flag x --flag y`` always works; with ``split=True`` (``x-split``) one value may also
    hold a comma separated list (``--flag x,y``). Free text and ``key=value`` lists never split."""
    out: dict[str, Any] = {"type": "array", "items": items or {"type": "string"}, "description": desc, **kw}
    if split:
        out["x-split"] = ","
    return out


def E(desc: str, *values: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": desc, **kw}


def REF(desc: str = "ticket REF (DEMO-0043, 43); default: your claim") -> dict[str, Any]:
    return S(desc, pattern=REF_PATTERN, **{"x-metavar": "REF"})


def TASK(desc: str = "task id (T3 or DEMO-0043/T3)") -> dict[str, Any]:
    return S(desc, pattern=TASK_REF_PATTERN, **{"x-metavar": "TASK"})


MSG = S("free text", **{"x-short": "m", "x-metavar": "TEXT"})
FILE = S("read the text from PATH, or - for stdin", **{"x-metavar": "PATH"})

# Output (data) building blocks.
STR = {"type": "string"}
INT = {"type": "integer", "minimum": 0}
BOOL = {"type": "boolean"}
KEY = {"$ref": "https://schemas.orch.dev/v2/common#/$defs/ticketKey"}


def obj(props: dict[str, Any] | None = None, optional: tuple[str, ...] = ()) -> dict[str, Any]:
    props = props or {}
    return {
        "type": "object",
        "properties": props,
        "required": [k for k in props if k not in optional],
        "additionalProperties": False,
    }


def arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def err(
    code: str, hint: str | None = None, fix: list[str] | None = None, retryable: bool | None = None
) -> dict[str, Any]:
    """An error entry of an operation. Hint, fix and retryable default to the catalog's."""
    return {"code": code, "hint": hint, "fix": fix, "retryable": retryable}


_IMPLIED = {
    "agent": ("grant.required", "grant.expired"),
    "unattended": ("grant.required", "grant.expired"),
    "human": ("human_only", "members.stale"),
}


def operation(
    name: str,
    group: str,
    summary: str,
    *,
    who: str,
    text: str,
    data: dict[str, Any] | None = None,
    props: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
    positional: tuple[str, ...] = (),
    one_of: tuple[str, ...] = (),
    pre: tuple[str, ...] = (),
    emits: tuple[str, ...] = (),
    errors: tuple[dict[str, Any], ...] = (),
    output_ref: str | None = None,
) -> Operation:
    """Build, complete and validate one declaration. ``errors`` lists the op's own codes; the ones implied by
    ``who`` (human_only, grant.required, ...) and by writing (lock.busy) are added, with catalog hint and fix."""
    from orch.ops import _unimplemented

    for p in pre:
        if p not in PRECONDITIONS:
            raise ValueError(f"{name}: unknown precondition {p!r}")
    declared = list(errors)
    present = {e["code"] for e in declared}
    extra = list(_IMPLIED.get(who, ()))
    if who != "read":
        extra.append("lock.busy")
    declared += [err(c) for c in extra if c not in present]
    entries = []
    for e in declared:
        code = e["code"]
        cat = ERRORS[code]
        if code in GLOBAL_ERRORS:
            raise ValueError(f"{name}: {code} is global, do not declare it")
        entry: dict[str, Any] = {
            "code": code,
            "hint": (e["hint"] or cat.hint).replace("{cmd}", name),
            "fix": {"argv": [a.replace("{cmd}", name) for a in (e["fix"] or cat.fix)]},
            "retryable": cat.retryable if e["retryable"] is None else e["retryable"],
        }
        entries.append(entry)
    entries.sort(key=lambda e: (ERRORS[e["code"]].exit, e["code"]))
    properties = dict(props or {})
    inp: dict[str, Any] = {"type": "object", "description": summary, "properties": properties}
    if positional:
        inp["x-positional"] = list(positional)
    inp["required"] = list(required)
    if one_of:  # exactly one of these must be given
        inp["oneOf"] = [{"required": [n]} for n in one_of]
    inp["additionalProperties"] = False
    out_json = {"$ref": output_ref} if output_ref else (data if data is not None else obj())
    decl = {
        "name": name,
        "input": inp,
        "who": who,
        "pre": list(pre),
        "emits": list(emits),
        "output": {"text": text, "json": out_json},
        "errors": entries,
    }
    assert cli_words(name)
    return Operation(declaration=decl, group=group, handler=_unimplemented(name))
