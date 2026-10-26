"""Parse widget structure and reproduce the dashboard's ticket-wide resolution."""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .core_types import KEYS, check_core, num, unknown
from .limits import MAX_BLOCK_BYTES, MAX_BLOCKS, MAX_STRING
from .strict import JS_SPACE, js_trim, strict_json, unicode_text

OTHER_TYPES = frozenset(
    (
        "text chips links health tests runs bullet scores gantt diffstat options matrix risk deps flow "
        "trail deploy compare screens video gallery preview review summary"
    ).split()
)
WIDGET_SECTIONS = frozenset(("context", "current_state", "verification"))
DEFAULT_ORDER = (
    "summary",
    "context",
    "requirements",
    "out_of_scope",
    "plan",
    "decisions",
    "verification",
    "current_state",
)
DEFAULT_LABELS = dict(
    zip(
        DEFAULT_ORDER,
        ("Summary", "Context", "Requirements", "Out of scope", "Plan", "Decisions", "Verification", "Current state"),
        strict=True,
    )
)


@dataclass
class BlockResult:
    spec: dict | None = None
    reason: str | None = None


@dataclass
class Block:
    section: str
    line: int
    raw: str
    spec: dict | None = None
    reason: str | None = None
    index: int | None = None


@dataclass
class MarkdownSegment:
    text: str
    kind: Literal["markdown"] = "markdown"


@dataclass
class WidgetSegment:
    block: Block
    kind: Literal["widget"] = "widget"


Segment = MarkdownSegment | WidgetSegment


def spec_to_json(spec):
    """Merge UTF-16 pairs for JSON consumers, retaining escaped lone surrogates."""
    if isinstance(spec, str):
        return unicode_text(spec)
    if isinstance(spec, dict):
        return {unicode_text(k): spec_to_json(v) for k, v in spec.items()}
    if isinstance(spec, list):
        return [spec_to_json(v) for v in spec]
    return spec


def segments_to_json(segments):
    if isinstance(segments, Mapping):
        return {k: segments_to_json(v) for k, v in segments.items()}
    out = []
    for s in segments:
        if s.kind == "markdown":
            out.append({"kind": "markdown", "text": s.text})
        else:
            b = s.block
            block = {"section": b.section, "line": b.line, "raw": b.raw}
            for k in ("reason", "spec", "index"):
                value = getattr(b, k)
                if value is not None:
                    block[k] = spec_to_json(value)
            out.append({"kind": "widget", "block": block})
    return out


def long_string(v):
    if isinstance(v, str):
        return len(v) > MAX_STRING
    if isinstance(v, list):
        return any(long_string(x) for x in v)
    if isinstance(v, dict):
        return any(len(k) > MAX_STRING or long_string(x) for k, x in v.items())
    return False


MAX_TOKEN = 60
MAX_REASON = 300
_QUOTED = re.compile(r'"([^"]*)"')


def clip_reason(reason: str) -> str:
    """A reason fit to show: every quoted token (a key, a name, a type taken from the ticket text) is cut to 60
    characters and the whole line to 300, so a refusal never echoes the content back. The dashboard keeps the full
    reason; the parity fixtures compare after the same clip."""

    def cut(m):
        t = m[1]
        return m[0] if len(t) <= MAX_TOKEN else '"' + t[: MAX_TOKEN - 3] + '..."'

    out = _QUOTED.sub(cut, reason)
    return out if len(out) <= MAX_REASON else out[: MAX_REASON - 3] + "..."


def parse_block(raw: str) -> BlockResult:
    """Never raise for agent-authored input, including unexpected internal failures."""
    try:
        result = _check(raw)
        if result.reason is not None:
            # JSON.stringify preserves lone surrogates in diagnostic strings too.
            result.reason = clip_reason(unicode_text(result.reason))
        return result
    except Exception:
        return BlockResult(reason="block could not be read")


def _check(raw):
    def bad(reason):
        return BlockResult(reason=reason)

    if len(unicode_text(raw, "replace").encode("utf-8")) > MAX_BLOCK_BYTES:
        return bad("block is larger than 64 KiB")
    try:
        v = strict_json(raw)
    except ValueError as e:
        return bad(f"invalid JSON: {e}")
    if not isinstance(v, dict):
        return bad("block must be a JSON object")
    layers = [k for k in ("type", "widget", "html") if k in v]
    if len(layers) != 1:
        return bad("block needs exactly one of type, widget or html")
    layer = layers[0]
    if long_string(v):
        return bad("a string is longer than 20000 characters")
    own = {
        "type": ["type"],
        "widget": ["widget", "sha256", "data"],
        "html": ["html", "sha256", "data", "libs", "height"],
    }[layer]
    type_keys = []
    if layer == "type":
        t = v["type"]
        if not isinstance(t, str):
            return bad("type must be a string")
        if t in OTHER_TYPES:
            return bad(f'type "{t}" is not drawn in this mockup')
        if t not in KEYS:
            return bad(f'unknown widget type "{t}"')
        type_keys = KEYS[t]
    r = unknown(v, ["id", "title", "source", "caption", *own, *type_keys])
    if r:
        return bad(r)
    spec = {"layer": layer, "fields": {}}
    for k, maximum in (("title", 200), ("source", 500), ("caption", 500)):
        if k not in v:
            continue
        if not isinstance(v[k], str):
            return bad(f"{k} must be a string")
        if len(v[k]) > maximum:
            return bad(f"{k} is longer than {maximum} characters")
        spec[k] = v[k]
    if "id" in v:
        if not isinstance(v["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", v["id"]):
            return bad("id must match [a-z][a-z0-9-]{0,39}")
        spec["id"] = v["id"]
    if layer == "type":
        spec["type"] = t
        r = check_core(t, v)
        if r:
            return bad(r)
        spec["fields"] = {k: v[k] for k in type_keys if k in v}
        return BlockResult(spec=spec)
    if "sha256" not in v:
        return bad("a template needs a sha256 pin" if layer == "widget" else "an html widget needs a sha256 pin")
    if not isinstance(v["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", v["sha256"]):
        return bad("sha256 must be 64 hex characters")
    spec["sha256"] = v["sha256"]
    if "data" in v:
        if not isinstance(v["data"], dict):
            return bad("data must be an object")
        spec["data"] = v["data"]
    if layer == "widget":
        if not isinstance(v["widget"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}@[1-9][0-9]{0,3}", v["widget"]):
            return bad("widget must be name@version, for example before-after@1")
        spec["widget"] = v["widget"]
        return BlockResult(spec=spec)
    path = v["html"] if isinstance(v["html"], str) else ""
    m = re.fullmatch(r"artifacts/([A-Z][A-Z0-9]*-\d+)/([^/]+)", path, re.ASCII) or re.fullmatch(
        r"artifact:([^/]+)", path
    )
    name = m.groups()[-1] if m else ""
    if not m or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", name):
        return bad("html must name a ticket artifact as artifacts/<ID>/<name> or artifact:<name>")
    spec.update(html=path, artifact=name)
    if len(m.groups()) == 2:
        spec["artifactTicket"] = m[1]
    if "height" in v:
        h = v["height"]
        if not num(h) or not h.is_integer() or not 40 <= h <= 2000:
            return bad("height must be an integer from 40 to 2000")
        spec["height"] = h
    if "libs" in v:
        libs = v["libs"]
        if (
            not isinstance(libs, list)
            or len(libs) > 8
            or not all(isinstance(x, str) and re.fullmatch(r"[a-z0-9-]{1,40}", x) for x in libs)
        ):
            return bad("libs must be a list of up to 8 library names")
        spec["libs"] = libs
    return BlockResult(spec=spec)


_OPEN = re.compile(r" {0,3}(`{3,}+|~{3,}+)([^\n\r\u2028\u2029]*)\Z")
_CLOSE = re.compile(r" {0,3}(`++|~++)[" + JS_SPACE + r"]*\Z")
_LAZY_BLOCK = re.compile(r"^(#{1,6}( |\Z)|>|([-*_])( *\3){2,} *\Z)")
_LIST = re.compile(r"^ {0,3}([-*+]|\d{1,9}[.)])( |\Z)", re.ASCII)


def parse_section(text: str, section: str) -> list[Segment]:
    lines = text.split("\n")
    out = []
    prose = []
    in_list = False
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _OPEN.fullmatch(line)
        if not m or (m[1][0] == "`" and "`" in m[2]):
            lazy = i > 0 and js_trim(lines[i - 1]) != "" and not _LAZY_BLOCK.match(line)
            if _LIST.match(line):
                in_list = True
            elif js_trim(line) and not line.startswith(" ") and not lazy:
                in_list = False
            prose.append(line)
            i += 1
            continue
        if not line.startswith(" "):
            in_list = False
        end = -1
        for j in range(i + 1, len(lines)):
            close = _CLOSE.fullmatch(lines[j])
            if close and close[1][0] == m[1][0] and len(close[1]) >= len(m[1]):
                end = j
                break
        last = len(lines) - 1 if end == -1 else end
        if js_trim(m[2]) != "orch" or (in_list and line.startswith(" ")):
            prose.extend(lines[i : last + 1])
            i = last + 1
            continue
        if prose:
            out.append(MarkdownSegment("\n".join(prose)))
            prose = []
        raw = "\n".join(lines[i + 1 : len(lines) if end == -1 else end])
        result = BlockResult(reason="fence is not closed") if end == -1 else parse_block(raw)
        out.append(WidgetSegment(Block(section, i + 1, raw, spec=result.spec, reason=result.reason)))
        i = last + 1
    if prose:
        out.append(MarkdownSegment("\n".join(prose)))
    return out


def resolve_ticket_widgets(body: Mapping[str, str | None], order=None, label=None) -> dict[str, list[Segment]]:
    order = DEFAULT_ORDER if order is None else order
    label_of = label if label is not None else lambda s: DEFAULT_LABELS.get(s, s)
    out = {k: parse_section(body[k], k) for k in order if body.get(k) and js_trim(body[k])}
    blocks = [s.block for k in order for s in out.get(k, []) if s.kind == "widget"]
    uses = Counter(b.spec["id"] for b in blocks if b.spec and b.spec.get("id"))
    index = 0
    for pos, b in enumerate(blocks):
        if not b.reason and b.spec and b.spec.get("id") and uses[b.spec["id"]] > 1:
            b.reason = f'id "{b.spec["id"]}" is used by more than one widget in this ticket'
        if not b.reason and b.section not in WIDGET_SECTIONS:
            b.reason = f"widgets are not drawn in {label_of(b.section)} (a gate hashes it or it has its own grammar)"
        if pos >= MAX_BLOCKS:
            b.reason = f"only the first {MAX_BLOCKS} widgets of a ticket are drawn"
        if not b.reason:
            b.index = index
            index += 1
    return out
