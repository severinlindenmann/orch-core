"""The YAML that v1 wrote, and nothing more (the v2 core has no YAML dependency).

v1 writes ticket frontmatter with ``yaml.safe_dump(default_flow_style=False, width=1000)``: block mappings and
sequences (sequences not indented under their key), ``[]`` and ``{}`` for empty containers, plain, single-quoted or
double-quoted scalars. A hand edit may add comments and flow collections of scalars. That is all this reads.

Anything else (anchors, aliases, tags, block scalars ``|`` and ``>``, multi-line flow collections, a tab as indent,
duplicate keys, nesting deeper than 12) raises :class:`YamlError`: the ticket is skipped and reported, never half
read. Scalars: ``null``, ``~`` and empty are ``None``; ``true`` and ``false`` are booleans; integers are ints;
everything else is a string (``yes``, ``no``, ``on``, ``off`` stay strings: a v1 option key ``no`` must not become
``False``). Timestamps stay strings, as in v1.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["YamlError", "loads"]

MAX_DEPTH = 12
MAX_LINES = 20_000


class YamlError(ValueError):
    pass


_INT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)")
_ESC = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "/": "/", " ": " ", "a": "\a", "b": "\b"}
_COMMENT = re.compile(r"(?:^|\s)#")


def loads(text: str) -> Any:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > MAX_LINES:
        raise YamlError("too many lines")
    p = _Parser(lines)
    p.skip()
    if p.i >= len(lines):
        return None
    indent = p.indent()
    value = p.block(indent, 0)
    p.skip()
    if p.i < len(lines):
        raise YamlError(f"line {p.i + 1}: unexpected content")
    return value


class _Parser:
    def __init__(self, lines: list[str]) -> None:
        self.lines, self.i = lines, 0

    # -- lines
    def skip(self) -> None:
        while self.i < len(self.lines):
            s = self.lines[self.i].strip()
            if s and not s.startswith("#"):
                if s == "---" and self.i == 0:
                    self.i += 1
                    continue
                return
            self.i += 1

    def indent(self) -> int:
        line = self.lines[self.i]
        n = len(line) - len(line.lstrip(" "))
        if line[n : n + 1] == "\t":
            raise YamlError(f"line {self.i + 1}: a tab is not an indent")
        return n

    # -- blocks
    def block(self, indent: int, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise YamlError("nested too deeply")
        text = self.lines[self.i][indent:]
        if text == "-" or text.startswith("- "):
            return self.sequence(indent, depth)
        if _split_key(text) is not None:
            return self.mapping(indent, depth)
        self.i += 1
        return self.scalar(text, indent)

    def sequence(self, indent: int, depth: int) -> list[Any]:
        if depth > MAX_DEPTH:
            raise YamlError("nested too deeply")
        out: list[Any] = []
        while True:
            self.skip()
            if self.i >= len(self.lines) or self.indent() != indent:
                break
            text = self.lines[self.i][indent:]
            if not (text == "-" or text.startswith("- ")):
                break
            rest = text[1:].lstrip(" ")
            if not rest or rest.startswith("#"):
                self.i += 1
                self.skip()
                if self.i < len(self.lines) and self.indent() > indent:
                    out.append(self.block(self.indent(), depth + 1))
                else:
                    out.append(None)
                continue
            gap = len(text) - len(rest)
            if rest == "-" or rest.startswith("- "):
                self.lines[self.i] = " " * (indent + gap) + rest
                out.append(self.sequence(indent + gap, depth + 1))
                continue
            if _split_key(rest) is not None and not rest.startswith(("'", '"', "[", "{")) or _quoted_key(rest):
                # "- key: value": the item is a mapping whose first key sits after the dash
                self.lines[self.i] = " " * (indent + gap) + rest
                out.append(self.mapping(indent + gap, depth + 1))
            else:
                self.i += 1
                out.append(self.scalar(rest, indent))
        return out

    def mapping(self, indent: int, depth: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            self.skip()
            if self.i >= len(self.lines) or self.indent() != indent:
                break
            text = self.lines[self.i][indent:]
            kv = _split_key(text)
            if kv is None or text.startswith("- "):
                break
            key, rest = kv
            if key in out:
                raise YamlError(f"line {self.i + 1}: duplicate key {key!r}")
            self.i += 1
            if not rest or rest.startswith("#"):
                self.skip()
                if self.i < len(self.lines) and self.indent() > indent:
                    out[key] = self.block(self.indent(), depth + 1)
                elif (
                    self.i < len(self.lines)
                    and self.indent() == indent
                    and (self.lines[self.i][indent:] == "-" or self.lines[self.i][indent:].startswith("- "))
                ):
                    out[key] = self.sequence(indent, depth + 1)  # PyYAML does not indent a sequence under its key
                else:
                    out[key] = None
            else:
                out[key] = self.scalar(rest, indent)
        return out

    # -- scalars
    def scalar(self, text: str, indent: int) -> Any:
        text = text.strip()
        if text[:1] in ("&", "*", "!", "|", ">", "%", "@", "`"):
            raise YamlError(f"line {self.i}: {text[:1]!r} (anchor, alias, tag or block scalar) is not supported")
        if text[:1] in ("'", '"'):
            return self.quoted(text, indent)
        if text[:1] in ("[", "{"):
            value, rest = _flow(text, 0, 0)
            rest = _strip_comment(rest.strip())
            if rest:
                raise YamlError(f"line {self.i}: text after a flow collection")
            return value
        return _plain(_strip_comment(text))

    def quoted(self, first: str, indent: int) -> str:
        """A quoted scalar that may continue on the following lines (single: a blank line is a newline)."""
        q = first[0]
        buf = first[1:]
        parts: list[str] = []
        while True:
            end = _closing(buf, q)
            if end is not None:
                parts.append(buf[:end])
                tail = buf[end + 1 :].strip()
                if tail and not tail.startswith("#"):
                    raise YamlError(f"line {self.i}: text after a quoted scalar")
                break
            parts.append(buf)
            if self.i >= len(self.lines):
                raise YamlError("a quoted scalar is not closed")
            buf = self.lines[self.i]
            self.i += 1
            parts.append("\x00")  # a line break inside the quotes
            buf = buf.strip()
        # fold: a single break is a space, k+1 breaks are k newlines
        raw = "".join(parts)
        folded = re.sub(r"(?:\x00)+", lambda m: " " if len(m.group()) == 1 else "\n" * (len(m.group()) - 1), raw)
        return _unquote(folded, q)


def _quoted_key(text: str) -> bool:
    if text[:1] not in ("'", '"'):
        return False
    end = _closing(text[1:], text[0])
    return end is not None and text[1 + end + 1 :].startswith(":")


def _closing(buf: str, q: str) -> int | None:
    i = 0
    while i < len(buf):
        c = buf[i]
        if q == '"' and c == "\\":
            i += 2
            continue
        if c == q:
            if q == "'" and buf[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return None


def _unquote(body: str, q: str) -> str:
    if q == "'":
        return body.replace("''", "'")
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        n = body[i + 1 : i + 2]
        if n in _ESC:
            out.append(_ESC[n])
            i += 2
        elif n in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[n]
            hexs = body[i + 2 : i + 2 + width]
            if len(hexs) != width or not re.fullmatch(r"[0-9a-fA-F]+", hexs):
                raise YamlError("bad escape in a double-quoted scalar")
            cp = int(hexs, 16)
            if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                raise YamlError("bad code point in a double-quoted scalar")
            out.append(chr(cp))
            i += 2 + width
        else:
            raise YamlError(f"unsupported escape \\{n}")
    return "".join(out)


def _split_key(text: str) -> tuple[str, str] | None:
    """``(key, rest)`` of a ``key: rest`` line, or ``None`` if it is no mapping entry."""
    if text[:1] in ("'", '"'):
        end = _closing(text[1:], text[0])
        if end is None or text[1 + end + 1 : 1 + end + 2] != ":":
            return None
        key = _unquote(text[1 : 1 + end], text[0])
        rest = text[1 + end + 2 :]
        return (key, rest.strip()) if rest == "" or rest[0] == " " else None
    m = re.match(r"([^\s:#\[\]{},&*!|>'\"%@`-][^:#]*?|-[^\s:#][^:#]*?):(?:\s+(.*))?$", text)
    if m is None:
        return None
    return m.group(1).strip(), (m.group(2) or "").strip()


def _strip_comment(text: str) -> str:
    m = _COMMENT.search(text)
    return text[: m.start()].rstrip() if m and (m.start() == 0 or text[m.start()].isspace()) else text.rstrip()


def _plain(text: str) -> Any:
    if text in ("", "~") or text.lower() == "null":
        return None
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if _INT.fullmatch(text):
        return int(text)
    return text


def _flow(text: str, pos: int, depth: int) -> tuple[Any, str]:
    """A flow sequence or mapping of scalars starting at ``text[pos]``; returns (value, rest of the line)."""
    if depth > MAX_DEPTH:
        raise YamlError("nested too deeply")
    close = "]" if text[pos] == "[" else "}"
    pos += 1
    items: list[Any] = []
    while True:
        pos = _ws(text, pos)
        if pos >= len(text):
            raise YamlError("a flow collection is not closed on its line")
        if text[pos] == close:
            pos += 1
            break
        value, pos = _flow_item(text, pos, depth, close)
        items.append(value)
        pos = _ws(text, pos)
        if pos < len(text) and text[pos] == ",":
            pos += 1
        elif pos >= len(text) or text[pos] != close:
            raise YamlError("expected a comma in a flow collection")
    if close == "]":
        return [i[1] if isinstance(i, _Pair) else i for i in items], text[pos:]
    out: dict[str, Any] = {}
    for it in items:
        if not isinstance(it, _Pair):
            raise YamlError("a flow mapping holds key: value pairs")
        if it[0] in out:
            raise YamlError(f"duplicate key {it[0]!r}")
        out[it[0]] = it[1]
    return out, text[pos:]


class _Pair(tuple):
    pass


def _ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] == " ":
        pos += 1
    return pos


def _flow_item(text: str, pos: int, depth: int, close: str) -> tuple[Any, int]:
    if text[pos] in "[{":
        value, rest = _flow(text, pos, depth + 1)
        return value, len(text) - len(rest)
    if text[pos] in ("'", '"'):
        q = text[pos]
        end = _closing(text[pos + 1 :], q)
        if end is None:
            raise YamlError("a quoted scalar is not closed on its line")
        value: Any = _unquote(text[pos + 1 : pos + 1 + end], q)
        pos = pos + 1 + end + 1
    else:
        start = pos
        while pos < len(text) and text[pos] not in ",:" + close and not (text[pos] == "#" and text[pos - 1] == " "):
            if text[pos] in "[{&*!|>":
                raise YamlError(f"{text[pos]!r} is not supported in a flow collection")
            pos += 1
        value = _plain(text[start:pos].strip())
    after = _ws(text, pos)
    if close == "}" and after < len(text) and text[after] == ":":
        v, after2 = _flow_value(text, after + 1, depth, close)
        if not isinstance(value, str):
            value = str(value)
        return _Pair((value, v)), after2
    return value, pos


def _flow_value(text: str, pos: int, depth: int, close: str) -> tuple[Any, int]:
    pos = _ws(text, pos)
    if pos >= len(text):
        raise YamlError("a flow mapping is not closed on its line")
    return _flow_item(text, pos, depth, close)
