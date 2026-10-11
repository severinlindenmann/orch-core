"""The ``needs`` rule language of an addon manifest (ticket-format §8.1): validation and a total evaluator.

An expression is JSON: a literal, or a list whose first element names an operator. There is no text to parse and no
code to run. :func:`validate_expr` is the load-time check; :func:`evaluate` never raises (a malformed expression, a
value of the wrong type, a limit hit: ``False``/``None``) and depends only on its two arguments, so it is
deterministic. A pure module: no I/O, no clock, no import of the model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["GATES", "MAX_DEPTH", "MAX_NODES", "MAX_STRING", "VARS", "NeedsRuleError", "evaluate", "validate_expr"]

MAX_DEPTH = 6
MAX_NODES = 40
MAX_STRING = 200
MAX_INT = 2**53 - 1
GATES = ("requirements", "plan", "verify", "code")
VARS = {
    "status": str,
    "type": str,
    "size": str,
    "priority": str,
    "labels": list,
    "open_questions": int,
    "tasks_open": int,
    "acceptance": int,
    "blocked": bool,
}
# operator -> (min operands, max operands or None)
_ARITY: dict[str, tuple[int, int | None]] = {
    "and": (1, None),
    "or": (1, None),
    "not": (1, 1),
    "eq": (2, 2),
    "ne": (2, 2),
    "lt": (2, 2),
    "le": (2, 2),
    "gt": (2, 2),
    "ge": (2, 2),
    "in": (2, None),
    "has": (2, 2),
    "is_null": (1, 1),
}
_REFS = ("var", "gate", "field")


class NeedsRuleError(ValueError):
    """An expression breaks the language of §8.1; the message names the position."""


def _is_int(x: Any) -> bool:
    return type(x) is int


def validate_expr(expr: Any, fields: frozenset[str] | set[str] | None = None) -> None:
    """Raise :class:`NeedsRuleError` unless ``expr`` is an expression of the language. ``fields`` are the field names of
    the manifest (``None`` skips that one check, for a caller that has no manifest)."""
    count = 0

    def walk(e: Any, depth: int, where: str) -> None:
        nonlocal count
        count += 1
        if count > MAX_NODES:
            raise NeedsRuleError(f"more than {MAX_NODES} nodes")
        if e is None or type(e) is bool:
            return
        if _is_int(e):
            if abs(e) > MAX_INT:
                raise NeedsRuleError(f"{where}: integer out of range")
            return
        if type(e) is str:
            if len(e) > MAX_STRING:
                raise NeedsRuleError(f"{where}: string longer than {MAX_STRING}")
            return
        if type(e) is not list or not e or type(e[0]) is not str:
            raise NeedsRuleError(f"{where}: not a literal and not [operator, ...]")
        if depth >= MAX_DEPTH:
            raise NeedsRuleError(f"{where}: deeper than {MAX_DEPTH}")
        op, args = e[0], e[1:]
        if op in _REFS:
            if len(args) != 1 or type(args[0]) is not str:
                raise NeedsRuleError(f"{where}: [{op!r}, NAME] takes one literal name")
            name = args[0]
            if op == "var" and name not in VARS:
                raise NeedsRuleError(f"{where}: unknown variable {name!r}")
            if op == "gate" and name not in GATES:
                raise NeedsRuleError(f"{where}: unknown gate {name!r}")
            if op == "field" and fields is not None and name not in fields:
                raise NeedsRuleError(f"{where}: {name!r} is not a field of this addon")
            count += 1
            return
        if op not in _ARITY:
            raise NeedsRuleError(f"{where}: unknown operator {op!r}")
        lo, hi = _ARITY[op]
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise NeedsRuleError(
                f"{where}: {op!r} takes {lo}{'' if hi == lo else '+' if hi is None else f'-{hi}'} operands"
            )
        for i, a in enumerate(args):
            walk(a, depth + 1, f"{where}/{i + 1}")

    walk(expr, 1, "when")


# ---------------------------------------------------------------------------------------------------- evaluation


class _Stop(Exception):
    """Internal: a limit was hit; the whole evaluation is ``False``."""


def evaluate(expr: Any, env: Mapping[str, Any], fields: Mapping[str, Any] | None = None) -> bool:
    """Whether the rule fires: ``expr`` evaluates to exactly ``True``. Total: never raises."""
    try:
        return _eval(expr, env, fields or {}, [0], 1) is True
    except Exception:  # noqa: BLE001  (totality is the contract: whatever went wrong, the rule does not fire)
        return False


def _eval(e: Any, env: Mapping[str, Any], fields: Mapping[str, Any], n: list[int], depth: int) -> Any:
    n[0] += 1
    if n[0] > MAX_NODES or depth > MAX_DEPTH + 1:
        raise _Stop
    if e is None or type(e) in (bool, int, str):
        return e
    if type(e) is not list or not e or type(e[0]) is not str:
        raise _Stop
    op, args = e[0], e[1:]
    if op in _REFS:
        if len(args) != 1 or type(args[0]) is not str:
            raise _Stop
        return _lookup(op, args[0], env, fields)
    arity = _ARITY.get(op)
    if arity is None or len(args) < arity[0] or (arity[1] is not None and len(args) > arity[1]):
        raise _Stop
    if op in ("and", "or"):
        vals = [_eval(a, env, fields, n, depth + 1) is True for a in args]
        return all(vals) if op == "and" else any(vals)
    vals = [_eval(a, env, fields, n, depth + 1) for a in args]
    if op == "not":
        return vals[0] is not True
    if op == "is_null":
        return vals[0] is None
    if op == "eq":
        return _same(vals[0], vals[1])
    if op == "ne":
        return not _same(vals[0], vals[1])
    if op == "in":
        return any(_same(vals[0], v) for v in vals[1:])
    if op == "has":
        return type(vals[0]) is list and any(_same(x, vals[1]) for x in vals[0])
    a, b = vals
    if not (_is_int(a) and _is_int(b)):
        return False
    return {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]


def _same(a: Any, b: Any) -> bool:
    """Equal type and value (``True`` is not ``1``); lists element by element."""
    if type(a) is not type(b):
        return False
    if type(a) is list:
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _lookup(kind: str, name: str, env: Mapping[str, Any], fields: Mapping[str, Any]) -> Any:
    if kind == "field":
        return _plain(fields.get(name))
    if kind == "gate":
        reached = env.get("gates")
        return bool(reached.get(name)) if isinstance(reached, Mapping) and name in GATES else False
    want = VARS.get(name)
    value = env.get(name)
    if want is None or type(value) is not want:  # a missing or mistyped input is null, never an error
        return None
    return _plain(value)


def _plain(v: Any) -> Any:
    """A value of the language: ``None``, bool, int, str or a list of those; anything else is ``None``."""
    if v is None or type(v) in (bool, int, str):
        return v
    if type(v) is list and all(x is None or type(x) in (bool, int, str) for x in v):
        return list(v)
    return None
