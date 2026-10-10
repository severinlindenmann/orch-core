"""Canonical JSON: ``cj`` and the strict parser (orch-relay protocol-v2 §2.3, ticket-format §11.2).

* :func:`dumps` is ``cj``: the restricted subset of protocol §2.3 (integers in +-(2^53 - 1), no floats, non-empty
  ASCII keys, no lone surrogates, nesting <= 16, root of any type) serialised as RFC 8785 (JCS) bytes. Everything
  that is signed, sealed or hashed in the core goes through it. Its output is byte-identical to Python's
  ``json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()`` on the subset. Inputs must
  be plain ``dict``/``list``/``str``/``int``/``bool``/``None`` (``type(x) is``, so subclasses such as
  ``OrderedDict`` or ``IntEnum`` are refused). It does **not** apply the text rules of §11.3 (those are checked by
  the hash and signing functions in :mod:`orch.canon.hashing`): ``cj`` of a string with a control character is
  the JSON-escaped form, as in the protocol.
* :func:`loads_strict` is the matching strict parser: it refuses duplicate keys, NaN/Infinity, floats (including
  ``1.0`` and ``1e3``), integers outside the safe range, non-ASCII keys, lone surrogates, invalid UTF-8, a BOM and
  over-deep nesting. ``-0`` parses to ``0``. It accepts any root type; callers that need an object (§11.2: the root
  of every file and line is an object) must check.
* :func:`validate` serialises the whole object once to check it; fine at the sizes of §11.2.

Nesting depth: the root container is level 1, so 16 nested containers are allowed and 17 are refused
(ticket-format §11.2). Scalars add no level.

There is deliberately no general RFC 8785 serialiser here: floats are a non-goal and a second serialiser next to
``cj`` invites signing with the wrong one. The RFC's number-formatting vectors are checked in the tests against an
oracle that lives in ``tests/canon``.
"""

from __future__ import annotations

import json
from typing import Any

MAX_SAFE_INT = 2**53 - 1
MAX_DEPTH = 16

__all__ = ["MAX_DEPTH", "MAX_SAFE_INT", "JcsError", "dumps", "loads_strict", "validate"]


class JcsError(ValueError):
    """The value is outside the canonical JSON subset, or the text is not strictly parseable."""


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise JcsError("string contains a lone surrogate") from None
    out = []
    for ch in s:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < " ":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _int(i: int) -> str:
    if not -MAX_SAFE_INT <= i <= MAX_SAFE_INT:
        raise JcsError("integer outside +-(2^53 - 1)")
    return str(i)


def _ser(o: Any, depth: int, out: list[str]) -> None:
    if o is None:
        out.append("null")
    elif o is True:
        out.append("true")
    elif o is False:
        out.append("false")
    elif type(o) is int:
        out.append(_int(o))
    elif type(o) is float:
        raise JcsError("floating-point numbers are not allowed")
    elif type(o) is str:
        out.append(_string(o))
    elif type(o) is list:
        if depth > MAX_DEPTH:
            raise JcsError("nesting deeper than 16 levels")
        out.append("[")
        for i, v in enumerate(o):
            if i:
                out.append(",")
            _ser(v, depth + 1, out)
        out.append("]")
    elif type(o) is dict:
        if depth > MAX_DEPTH:
            raise JcsError("nesting deeper than 16 levels")
        for k in o:
            if type(k) is not str:
                raise JcsError("object keys must be strings")
            if not k or not k.isascii():
                raise JcsError("object keys must be non-empty ASCII")
        out.append("{")
        # Keys are ASCII, so code point order equals RFC 8785's UTF-16 code unit order.
        for i, k in enumerate(sorted(o)):
            if i:
                out.append(",")
            out.append(_string(k))
            out.append(":")
            _ser(o[k], depth + 1, out)
        out.append("}")
    else:
        raise JcsError(f"type {type(o).__name__} is not JSON")


def dumps(obj: Any) -> bytes:
    """The protocol's ``cj(obj)``: canonical UTF-8 bytes of an object in the restricted subset."""
    out: list[str] = []
    _ser(obj, 1, out)
    return "".join(out).encode("utf-8")


def validate(obj: Any) -> None:
    """Raise :class:`JcsError` unless ``obj`` is in the restricted subset."""
    dumps(obj)


def _reject_constant(name: str) -> Any:
    raise JcsError(f"{name} is not allowed")


def _reject_float(text: str) -> Any:
    raise JcsError(f"floating-point number {text!r} is not allowed")


def _parse_int(text: str) -> int:
    i = int(text)
    if not -MAX_SAFE_INT <= i <= MAX_SAFE_INT:
        raise JcsError("integer outside +-(2^53 - 1)")
    return i


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise JcsError(f"duplicate key {k!r}")
        d[k] = v
    return d


def loads_strict(data: bytes | str) -> Any:
    """Parse JSON text under the strict rules of protocol-v2 §2.3; raise :class:`JcsError` on any violation."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            raise JcsError("invalid UTF-8") from None
    elif isinstance(data, str):
        text = data
    else:
        raise JcsError(f"JSON text must be str or bytes, not {type(data).__name__}")
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_reject_float,
            parse_int=_parse_int,
            parse_constant=_reject_constant,
        )
    except JcsError:
        raise
    except (ValueError, RecursionError) as e:  # JSONDecodeError, int digit limit, too deep
        raise JcsError(f"invalid JSON: {e}") from None
    validate(obj)  # non-ASCII/empty keys, lone surrogates, depth
    return obj
