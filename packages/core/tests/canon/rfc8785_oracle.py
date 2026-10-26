"""Test-only oracle: a general RFC 8785 serialiser (floats, any string keys).

It exists to check the number and key-sorting rules against the RFC's own test data. It is deliberately not in
``orch.canon``: floats are a non-goal of the format and a second serialiser next to ``cj`` invites signing with
the wrong one. It has no depth limit and accepts ints beyond 2^53 exactly; it is not a model for production code.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def es_number(f: float) -> str:
    """ECMAScript Number::toString (RFC 8785 §3.2.2.3) for a finite double."""
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("NaN and Infinity are not JSON")
    if f == 0:
        return "0"
    sign = "-" if f < 0 else ""
    t = Decimal(repr(abs(f))).as_tuple()  # repr is the shortest round-tripping digit string
    full = "".join(map(str, t.digits))
    digits = full.rstrip("0")
    k = len(digits)
    n = k + t.exponent + (len(full) - k)  # value = 0.<digits> * 10^n
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        es = ("+" if e >= 0 else "-") + str(abs(e))
        body = digits + "e" + es if k == 1 else digits[0] + "." + digits[1:] + "e" + es
    return sign + body


def _string(s: str) -> str:
    return '"' + "".join(_ESCAPES.get(c) or (f"\\u{ord(c):04x}" if c < " " else c) for c in s) + '"'


def _ser(o: Any, out: list[str]) -> None:
    if o is None:
        out.append("null")
    elif o is True:
        out.append("true")
    elif o is False:
        out.append("false")
    elif isinstance(o, int):
        out.append(str(o))
    elif isinstance(o, float):
        out.append(es_number(o))
    elif isinstance(o, str):
        out.append(_string(o))
    elif isinstance(o, list):
        out.append("[")
        for i, v in enumerate(o):
            if i:
                out.append(",")
            _ser(v, out)
        out.append("]")
    elif isinstance(o, dict):
        out.append("{")
        for i, k in enumerate(sorted(o, key=lambda s: s.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(_string(k) + ":")
            _ser(o[k], out)
        out.append("}")
    else:
        raise TypeError(type(o).__name__)


def dumps_general(obj: Any) -> bytes:
    out: list[str] = []
    _ser(obj, out)
    return "".join(out).encode("utf-8")
