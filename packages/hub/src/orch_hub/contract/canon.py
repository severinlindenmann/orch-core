"""Canonical JSON and text forms of bytes, exactly as orch-relay protocol-v2 §2.2-§2.3 (no second encoding).

``cj`` is the restricted subset serialised as RFC 8785 bytes: integers in +-(2^53 - 1), no floats, non-empty ASCII
keys, no lone surrogates, nesting <= 16. It is byte-identical to orch-core's ``orch.canon.jcs.dumps`` and to
``json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()`` on the subset.
``loads_strict`` refuses duplicate keys, NaN/Infinity, floats, unsafe integers, non-ASCII keys, invalid UTF-8 and
over-deep nesting. ``b64u``/``unb64u``/``unhex`` are protocol-v2 §2.2 (canonical unpadded base64url; lower-case hex).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

MAX_SAFE_INT = 2**53 - 1
MAX_DEPTH = 16


class CanonError(ValueError):
    """Outside the canonical subset, or not strictly parseable, or not the one canonical text form."""


_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise CanonError("lone surrogate") from None
    out = []
    for ch in s:
        e = _ESC.get(ch)
        out.append(e if e is not None else (f"\\u{ord(ch):04x}" if ch < " " else ch))
    return '"' + "".join(out) + '"'


def _ser(o: Any, depth: int, out: list[str]) -> None:
    if o is None:
        out.append("null")
    elif o is True:
        out.append("true")
    elif o is False:
        out.append("false")
    elif type(o) is int:
        if not -MAX_SAFE_INT <= o <= MAX_SAFE_INT:
            raise CanonError("integer outside +-(2^53 - 1)")
        out.append(str(o))
    elif type(o) is str:
        out.append(_string(o))
    elif type(o) is list:
        if depth > MAX_DEPTH:
            raise CanonError("nesting deeper than 16")
        out.append("[")
        for i, v in enumerate(o):
            if i:
                out.append(",")
            _ser(v, depth + 1, out)
        out.append("]")
    elif type(o) is dict:
        if depth > MAX_DEPTH:
            raise CanonError("nesting deeper than 16")
        for k in o:
            if type(k) is not str or not k or not k.isascii():
                raise CanonError("object keys must be non-empty ASCII strings")
        out.append("{")
        for i, k in enumerate(sorted(o)):
            if i:
                out.append(",")
            out.append(_string(k))
            out.append(":")
            _ser(o[k], depth + 1, out)
        out.append("}")
    else:
        raise CanonError(f"type {type(o).__name__} is not in the canonical subset")


def cj(o: Any) -> bytes:
    """protocol-v2 §2.3 ``cj(o)``."""
    out: list[str] = []
    _ser(o, 1, out)
    return "".join(out).encode("utf-8")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise CanonError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _no_float(text: str) -> Any:
    raise CanonError("floating-point number")


def _no_const(text: str) -> Any:
    raise CanonError("NaN/Infinity")


def _int(text: str) -> int:
    i = int(text)
    if not -MAX_SAFE_INT <= i <= MAX_SAFE_INT:
        raise CanonError("integer outside +-(2^53 - 1)")
    return i


def loads_strict(data: bytes | str) -> Any:
    """Strict parse (protocol-v2 §2.3). Any violation raises :class:`CanonError`."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            raise CanonError("invalid UTF-8") from None
    if not isinstance(data, str):
        raise CanonError("JSON text must be str or bytes")
    try:
        obj = json.loads(
            data, object_pairs_hook=_pairs, parse_float=_no_float, parse_int=_int, parse_constant=_no_const
        )
    except CanonError:
        raise
    except (ValueError, RecursionError) as e:
        raise CanonError(f"invalid JSON: {e}") from None
    cj(obj)  # keys, surrogates, depth
    return obj


_B64U = re.compile(r"[A-Za-z0-9_-]*")
_HEX = re.compile(r"(?:[0-9a-f]{2})*")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(s: object, n: int | None = None) -> bytes:
    """Canonical unpadded base64url; ``n`` is the exact decoded length where the field has one."""
    if type(s) is not str or not _B64U.fullmatch(s) or len(s) % 4 == 1:
        raise CanonError("not canonical base64url")
    data = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if b64u(data) != s:
        raise CanonError("non-canonical base64url (trailing bits)")
    if n is not None and len(data) != n:
        raise CanonError(f"expected {n} bytes")
    return data


def unhex(s: object, n: int) -> bytes:
    """Lower-case hex of exactly ``n`` bytes."""
    if type(s) is not str or len(s) != 2 * n or not _HEX.fullmatch(s):
        raise CanonError(f"not lower-case hex of {n} bytes")
    return bytes.fromhex(s)
