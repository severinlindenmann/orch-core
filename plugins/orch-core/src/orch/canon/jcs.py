"""Canonical JSON.

Two serialisers, both emitting the byte sequence defined by RFC 8785 (JCS):

* :func:`dumps` is the protocol's ``cj``: the restricted subset of ``orch-relay/docs/protocol-v2.md`` §2.3
  (integers in +-(2^53 - 1), no floats, non-empty ASCII keys, no lone surrogates, nesting <= 16). Everything that
  is signed, sealed or hashed in the core goes through it (ticket-format §5: signing and gate hash).
  Its output is byte-identical to Python's
  ``json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()`` on the subset.
* :func:`loads_strict` is the matching strict parser of §2.3: it refuses duplicate keys, NaN/Infinity, floats
  (including ``1.0`` and ``1e3``), integers outside the safe range, non-ASCII keys, lone surrogates, invalid
  UTF-8 and over-deep nesting.
* :func:`dumps_general` is the full RFC 8785 serialiser (floats in ECMAScript form, any string keys sorted by
  UTF-16 code units). It is not used for anything signed; it exists so the implementation is checked against
  the RFC's own test data.

Nesting depth: the root container counts as level 1, so 16 nested containers are allowed and 17 are refused
(the protocol does not say how levels are counted; see the PR's open questions).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

MAX_SAFE_INT = 2**53 - 1
MAX_DEPTH = 16

__all__ = ["MAX_DEPTH", "MAX_SAFE_INT", "JcsError", "dumps", "dumps_general", "loads_strict", "validate"]


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


def _es_number(f: float) -> str:
    """ECMAScript Number::toString (RFC 8785 §3.2.2.3) for a finite double."""
    if f != f or f in (float("inf"), float("-inf")):
        raise JcsError("NaN and Infinity are not JSON")
    if f == 0:
        return "0"
    sign = "-" if f < 0 else ""
    t = Decimal(repr(abs(f))).as_tuple()  # repr is the shortest round-tripping digit string
    full = "".join(map(str, t.digits))
    digits = full.rstrip("0")
    k = len(digits)
    n = k + t.exponent + (len(full) - k)  # value = 0.<digits> * 10^n, i.e. the decimal point sits after n digits
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


def _int(i: int) -> str:
    if not -MAX_SAFE_INT <= i <= MAX_SAFE_INT:
        raise JcsError("integer outside +-(2^53 - 1)")
    return str(i)


def _ser(o: Any, depth: int, general: bool, out: list[str]) -> None:
    if o is None:
        out.append("null")
    elif o is True:
        out.append("true")
    elif o is False:
        out.append("false")
    elif type(o) is int:
        out.append(_int(o))
    elif type(o) is float:
        if not general:
            raise JcsError("floating-point numbers are not allowed")
        out.append(_es_number(o))
    elif type(o) is str:
        out.append(_string(o))
    elif type(o) is list:
        if not general and depth > MAX_DEPTH:
            raise JcsError("nesting deeper than 16 levels")
        out.append("[")
        for i, v in enumerate(o):
            if i:
                out.append(",")
            _ser(v, depth + 1, general, out)
        out.append("]")
    elif type(o) is dict:
        if not general and depth > MAX_DEPTH:
            raise JcsError("nesting deeper than 16 levels")
        for k in o:
            if type(k) is not str:
                raise JcsError("object keys must be strings")
            if not general and (not k or not k.isascii()):
                raise JcsError("object keys must be non-empty ASCII")
        out.append("{")
        # Sorting by UTF-16 code units == comparing the big-endian UTF-16 encodings.
        for i, k in enumerate(sorted(o, key=lambda s: _utf16(s))):
            if i:
                out.append(",")
            out.append(_string(k))
            out.append(":")
            _ser(o[k], depth + 1, general, out)
        out.append("}")
    else:
        raise JcsError(f"type {type(o).__name__} is not JSON")


def _utf16(s: str) -> bytes:
    try:
        return s.encode("utf-16-be")
    except UnicodeEncodeError:
        raise JcsError("string contains a lone surrogate") from None


def dumps(obj: Any) -> bytes:
    """The protocol's ``cj(obj)``: canonical UTF-8 bytes of an object in the restricted subset."""
    out: list[str] = []
    _ser(obj, 1, False, out)
    return "".join(out).encode("utf-8")


def dumps_general(obj: Any) -> bytes:
    """Full RFC 8785 canonical JSON (floats allowed, any string keys). Not for signed data."""
    out: list[str] = []
    _ser(obj, 1, True, out)
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
    else:
        text = data
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
