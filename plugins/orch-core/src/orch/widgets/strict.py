"""Strict JSON with JavaScript string, number and object-order semantics."""

import json
import math
import re

from .limits import MAX_DEPTH

JS_SPACE = (
    " \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
MISSING = object()


def utf16(s: str) -> str:
    b = s.encode("utf-16-le", "surrogatepass")
    return "".join(chr(b[i] | b[i + 1] << 8) for i in range(0, len(b), 2))


def unicode_text(s: str, errors="surrogatepass") -> str:
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", errors)


def js_trim(s: str) -> str:
    return s.strip(JS_SPACE)


def js_order(o: dict) -> dict:
    indices = sorted(
        (k for k in o if re.fullmatch(r"0|[1-9][0-9]*", k) and len(k) <= 10 and int(k) < 2**32 - 1), key=int
    )
    if not indices:
        return o
    seen = set(indices)
    return {k: o[k] for k in [*indices, *(k for k in o if k not in seen)]}


def js_string(v) -> str:
    if v is MISSING:
        return "undefined"
    if v is None:
        return "null"
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ",".join("" if x is None else js_string(x) for x in v)
    if isinstance(v, dict):
        raise TypeError("Cannot convert a prototype-free object to primitive value")
    if v == 0:
        return "0"
    s = repr(float(v)).lower()
    if "e" in s:
        mantissa, exponent = s.split("e")
        exp = int(exponent)
        if -6 <= exp < 21:
            from decimal import Decimal

            return format(Decimal(s), "f").rstrip("0").rstrip(".") if exp < 0 else format(Decimal(s), "f").split(".")[0]
        return mantissa.removesuffix(".0") + "e" + ("+" if exp >= 0 else "-") + str(abs(exp))
    return s.removesuffix(".0")


_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?", re.ASCII)
_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')


def strict_json(text: str, max_depth: int = MAX_DEPTH):
    """Parse doubles and UTF-16 strings; reject containers before descending past the cap."""
    text = utf16(text)
    i = 0

    def fail(message):
        raise ValueError(message)

    def char():
        return text[i] if i < len(text) else None

    def ws():
        nonlocal i
        while i < len(text) and text[i] in " \t\n\r":
            i += 1

    def string():
        nonlocal i
        m = _STRING.match(text, i)
        if not m:
            fail(f"bad string at position {i}")
        i = m.end()
        return utf16(json.loads(m[0]))

    def value(depth):
        nonlocal i
        ws()
        c = char()
        if c in ("{", "["):
            if depth + 1 > max_depth:
                fail(f"nesting deeper than {max_depth} levels")
            is_object = c == "{"
            i += 1
            out = {} if is_object else []
            end = "}" if is_object else "]"
            ws()
            if char() == end:
                i += 1
                return out
            while True:
                if is_object:
                    ws()
                    if char() != '"':
                        fail(f"expected a key at position {i}")
                    key = string()
                    if key in out:
                        fail(f'duplicate key "{key}"')
                    ws()
                    if char() != ":":
                        fail(f'expected ":" at position {i}')
                    i += 1
                    out[key] = value(depth + 1)
                else:
                    out.append(value(depth + 1))
                ws()
                if char() == ",":
                    i += 1
                    continue
                if char() == end:
                    i += 1
                    return js_order(out) if is_object else out
                fail(f'expected "," or "{end}" at position {i}')
        if c == '"':
            return string()
        for token, result in (("true", True), ("false", False), ("null", None)):
            if text.startswith(token, i):
                i += len(token)
                return result
        if re.match(r"-?(NaN|Infinity)", text[i : i + 10]):
            fail("NaN and Infinity are not allowed")
        m = _NUMBER.match(text, i)
        if not m:
            fail("unexpected end" if i >= len(text) else f'unexpected "{text[i]}" at position {i}')
        i = m.end()
        n = float(m[0])
        if not math.isfinite(n):
            fail("NaN and Infinity are not allowed")
        return n

    result = value(0)
    ws()
    if i < len(text):
        fail(f'unexpected "{text[i]}" at position {i}')
    return result
