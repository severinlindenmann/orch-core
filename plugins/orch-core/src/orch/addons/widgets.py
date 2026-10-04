"""Widgets (spec v2 §11.5): plain data that core templates render with autoescape. No HTML, ever."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Union

ROLES = ("ok", "info", "warn", "err", "neu")  # no "you": addon items never count as needs-you
_SAFE_URL = re.compile(r"(?:https?://[^\s\\]+|/(?![/\\])[^\s\\]*)")
MAX_DEPTH = 2  # Card > Card > Card at most
MAX_TEXT_LEN = 20000
MAX_MARKDOWN_LEN = 262144  # a 256 KiB page
MAX_LABEL_LEN = 200
MAX_TARGET_LEN = 500
MAX_ROWS = 500
MAX_BODY_ITEMS = 500
MAX_VALUE_LEN = 200
MAX_URL_LEN = 2000
MAX_COLUMNS = 50
MAX_QR_LEN = 1000
_CORE_PARAMS = frozenset({"msg", "err", "token"})  # flash messages and the login token, never an addon's
_PARAM = re.compile(r"[a-z][a-z0-9_]{0,49}")  # a Search field name: also a GET query key on the addon page


def safe_url(url) -> bool:
    return isinstance(url, str) and _SAFE_URL.fullmatch(url) is not None


@dataclass(frozen=True)
class Text:
    text: str
    kind: ClassVar[str] = "text"


@dataclass(frozen=True)
class Markdown:
    """API 2.2: a block of Markdown (a wiki page, release notes) drawn by core through the same safe renderer and R24
    link rules as ticket text: raw HTML is shown as text, hidden characters are made visible, and it never has an
    artifact scope, so no link or image to a ticket artifact is live (no gate hash covers addon text)."""
    text: str
    here: str = ""  # the id of the page shown, for relative links (see pages)
    pages: tuple = ()  # ids of your pages: a relative link to one of them opens /addons/<name>/?page=<id>
    kind: ClassVar[str] = "markdown"


@dataclass(frozen=True)
class Badge:
    role: str
    text: str
    kind: ClassVar[str] = "badge"


@dataclass(frozen=True)
class Link:
    text: str
    url: str
    current: bool = False  # in a Chips row: the filter that is shown now (drawn selected, aria-current)
    kind: ClassVar[str] = "link"


@dataclass(frozen=True)
class Copy:
    label: str
    text: str
    kind: ClassVar[str] = "copy"


@dataclass(frozen=True)
class Action:
    action: str  # an id from the manifest's actions
    label: str
    target: str = ""
    confirm: str | None = None  # overrides the manifest's confirm text for this one instance; None keeps the manifest's
    quiet: bool = False  # API 2.1: the lesser action of a row or card (Ignore, Dismiss), drawn without a border
    kind: ClassVar[str] = "action"


@dataclass(frozen=True)
class Callout:
    role: str
    title: str
    text: str = ""
    kind: ClassVar[str] = "callout"


TIME_STYLES = ("ago", "at")


@dataclass(frozen=True)
class Time:
    """API 2.1: a timestamp core formats (never a formatted string from the addon). `at` is ISO 8601 with a
    UTC offset or Z; style "ago" reads "2 min ago", "at" reads "03.10. 14:32"; both carry the full time as a
    tooltip and a machine-readable <time datetime>. (The spec's `kind=` is `style=` here: `kind` is every
    widget's type name.)"""
    at: str
    style: str = "ago"
    kind: ClassVar[str] = "time"


Cell = Union[str, int, float, None, Text, Badge, Link, Copy, Action, Time]


KV_LAYOUTS = (None, "stats")
CARD_LAYOUTS = (None, "grid")


@dataclass(frozen=True)
class KV:
    rows: tuple
    layout: str | None = None  # "stats": a row of big values over small labels (a few counts on a card)
    kind: ClassVar[str] = "kv"


@dataclass(frozen=True)
class Chips:
    """A wrapping row of Badge, Link and Text items. Links read as filter chips; `Link(current=True)` marks
    the one shown now. `label` names the row for screen readers (e.g. "Filter by state")."""
    items: tuple
    label: str = ""
    kind: ClassVar[str] = "chips"


@dataclass(frozen=True)
class Table:
    columns: tuple
    rows: tuple
    empty: str = "Nothing here."
    key: int = 0  # API 2.1: the column that names the row (the title of a stacked row, the sticky column)
    kind: ClassVar[str] = "table"


@dataclass(frozen=True)
class Tabs:
    """API 2.1, addon page only: same-page views as core tab pills. Each item is a Link to the page with a
    query; `Link(current=True)` marks the view shown now. `label` names the tab row for screen readers."""
    items: tuple
    label: str = ""
    kind: ClassVar[str] = "tabs"


@dataclass(frozen=True)
class Card:
    title: str
    body: tuple = ()
    role: str | None = None
    href: str | None = None
    layout: str | None = None  # "grid": the child Cards sit in a responsive grid (compact cards side by side)
    kind: ClassVar[str] = "card"


@dataclass(frozen=True)
class Search:
    """A plain GET form on the addon's own page; the submitted value comes back as `view.params[name]`."""
    name: str
    value: str = ""
    placeholder: str = ""
    kind: ClassVar[str] = "search"


@dataclass(frozen=True)
class Tile:
    label: str
    value: Union[str, int, float, None]
    role: str = "neu"
    href: str | None = None
    sub: str = ""
    kind: ClassVar[str] = "tile"


@dataclass(frozen=True)
class QR:
    """A QR code; the dashboard draws it server-side as an inline SVG from a module matrix (no JS,
    no external URL). Dashboard-extra only (segno). `uncached` is core's own flag (never set by an
    addon): core's pairing-link QR carries a one-time secret and must not sit in the module-matrix
    cache, so it renders through the uncached path instead."""
    text: str
    caption: str = ""
    uncached: bool = False
    kind: ClassVar[str] = "qr"


_CELLS = (Text, Badge, Link, Copy, Action, Time)
_BLOCKS = (Text, Markdown, Badge, Link, Copy, Action, Callout, KV, Table, Card, Search, Chips, QR, Tabs, Time)
_CHIP_ITEMS = (Text, Badge, Link)
_SCALAR = (str, int, float)  # a `Cell`/`Tile.value` scalar; bool is deliberately excluded below


def _is_scalar(value) -> bool:
    return value is None or (isinstance(value, _SCALAR) and not isinstance(value, bool))


def _str_field(value, cap: int, where: str, field: str, out: list[str]) -> bool:
    """A required string field, within `cap` characters. Appends a problem and returns False instead
    of ever raising, so a malformed value (wrong type, too long) never crashes the check."""
    if not isinstance(value, str):
        out.append(f"{where}.{field}: must be a string, got {type(value).__name__}")
        return False
    if len(value) > cap:
        out.append(f"{where}.{field}: must be at most {cap} characters, got {len(value)}")
        return False
    return True


def _title_field(value, cap: int, where: str, field: str, out: list[str]) -> None:
    """A string field that names something (Badge text, Callout and Card titles, Tile label): never blank (E1)."""
    if _str_field(value, cap, where, field, out) and not value.strip():
        out.append(f"{where}.{field}: must not be empty")


def _flag(value, where: str, field: str, out: list[str]) -> None:
    if not isinstance(value, bool):
        out.append(f"{where}.{field}: must be True or False, got {type(value).__name__}")


def _iso_time(value: str) -> bool:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _role_field(value, where: str, out: list[str]) -> None:
    if value not in ROLES:
        out.append(f"{where}: role {value!r} must be one of {', '.join(ROLES)}")


def _url_field(value, where: str, label: str, out: list[str]) -> None:
    if not safe_url(value):
        out.append(f"{where}: {label} {value!r} must be http(s) or a same-origin path")
        return
    if len(value) > MAX_URL_LEN:
        out.append(f"{where}: {label} must be at most {MAX_URL_LEN} characters, got {len(value)}")


def _cell(value, manifest, where: str, out: list[str]) -> None:
    if _is_scalar(value):
        return
    if isinstance(value, _CELLS):
        _check(value, manifest, where, out, 0)
        return
    out.append(f"{where}: {type(value).__name__} is not allowed in a cell")


def _rows_of(value, where: str, field: str, out: list[str]) -> list:
    """`value` as a bounded list of rows, or `[]` plus a problem — never raises, even for `None` or a
    non-sequence (a malformed container such as `KV(None)`)."""
    if not isinstance(value, (tuple, list)):
        out.append(f"{where}.{field}: must be a tuple of rows, got {type(value).__name__}")
        return []
    rows = list(value)
    if len(rows) > MAX_ROWS:
        out.append(f"{where}.{field}: at most {MAX_ROWS} rows, got {len(rows)}")
        rows = rows[:MAX_ROWS]
    return rows


def _check(w, manifest, where: str, out: list[str], depth: int, slot: str = "") -> None:
    if depth > MAX_DEPTH:
        out.append(f"{where}: widgets nested deeper than {MAX_DEPTH} levels")
        return

    if isinstance(w, Text):
        _str_field(w.text, MAX_TEXT_LEN, where, "text", out)

    elif isinstance(w, Markdown):
        _str_field(w.text, MAX_MARKDOWN_LEN, where, "text", out)
        _str_field(w.here, MAX_TARGET_LEN, where, "here", out)
        if not isinstance(w.pages, tuple) or len(w.pages) > MAX_ROWS:
            out.append(f"{where}.pages: must be a tuple of at most {MAX_ROWS} page ids")
        else:
            for i, pid in enumerate(w.pages):
                _str_field(pid, MAX_TARGET_LEN, where, f"pages[{i}]", out)

    elif isinstance(w, Badge):
        _role_field(w.role, where, out)
        _title_field(w.text, MAX_TEXT_LEN, where, "text", out)

    elif isinstance(w, Time):
        if _str_field(w.at, MAX_VALUE_LEN, where, "at", out) and not _iso_time(w.at):
            out.append(f"{where}.at: must be an ISO 8601 time with a UTC offset or Z, got {w.at!r}")
        if w.style not in TIME_STYLES:
            out.append(f"{where}.style: {w.style!r} must be one of {', '.join(TIME_STYLES)}")

    elif isinstance(w, Tabs):
        if not slot.startswith("page."):
            out.append(f"{where}: Tabs is only allowed on the addon's page (slot page.<name>)")
        _str_field(w.label, MAX_LABEL_LEN, where, "label", out)
        for i, item in enumerate(_rows_of(w.items, where, "items", out)):
            if not isinstance(item, Link):
                out.append(f"{where}.items[{i}]: {type(item).__name__} is not allowed in Tabs (Link only)")
                continue
            _check(item, manifest, f"{where}.items[{i}]", out, depth)

    elif isinstance(w, Link):
        _str_field(w.text, MAX_LABEL_LEN, where, "text", out)
        _url_field(w.url, where, "url", out)
        if not isinstance(w.current, bool):
            out.append(f"{where}.current: must be True or False, got {type(w.current).__name__}")

    elif isinstance(w, Copy):
        _str_field(w.label, MAX_LABEL_LEN, where, "label", out)
        _str_field(w.text, MAX_TEXT_LEN, where, "text", out)

    elif isinstance(w, Action):
        _str_field(w.label, MAX_LABEL_LEN, where, "label", out)
        _str_field(w.target, MAX_TARGET_LEN, where, "target", out)
        if w.confirm is not None:
            _str_field(w.confirm, MAX_TEXT_LEN, where, "confirm", out)
        _flag(w.quiet, where, "quiet", out)
        if not isinstance(w.action, str):
            out.append(f"{where}.action: must be a string, got {type(w.action).__name__}")
        elif manifest.action(w.action) is None:
            out.append(f"{where}: action {w.action!r} is not declared in the manifest")

    elif isinstance(w, Search):
        if not slot.startswith("page."):
            out.append(f"{where}: Search is only allowed on the addon's page (slot page.<name>)")
        if not isinstance(w.name, str) or not _PARAM.fullmatch(w.name):
            out.append(f"{where}.name: must match ^[a-z][a-z0-9_]*$ (at most 50 characters)")
        elif w.name in _CORE_PARAMS:
            out.append(f"{where}.name: {w.name!r} is core's own query key (msg, err, token); pick another name")
        _str_field(w.value, MAX_VALUE_LEN, where, "value", out)
        _str_field(w.placeholder, MAX_LABEL_LEN, where, "placeholder", out)

    elif isinstance(w, QR):
        if _str_field(w.text, MAX_QR_LEN, where, "text", out) and len(w.text.encode("utf-8")) > MAX_QR_LEN:
            out.append(f"{where}.text: must be at most {MAX_QR_LEN} bytes as UTF-8, got {len(w.text.encode('utf-8'))}")
        _str_field(w.caption, MAX_LABEL_LEN, where, "caption", out)

    elif isinstance(w, Callout):
        _role_field(w.role, where, out)
        _title_field(w.title, MAX_LABEL_LEN, where, "title", out)
        _str_field(w.text, MAX_TEXT_LEN, where, "text", out)

    elif isinstance(w, Chips):
        _str_field(w.label, MAX_LABEL_LEN, where, "label", out)
        for i, item in enumerate(_rows_of(w.items, where, "items", out)):
            if not isinstance(item, _CHIP_ITEMS):
                out.append(f"{where}.items[{i}]: {type(item).__name__} is not allowed in Chips (Badge, Link or Text)")
                continue
            _check(item, manifest, f"{where}.items[{i}]", out, depth)

    elif isinstance(w, KV):
        if w.layout not in KV_LAYOUTS:
            out.append(f"{where}: layout {w.layout!r} must be one of None, 'stats'")
        for i, row in enumerate(_rows_of(w.rows, where, "rows", out)):
            if not (isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], str)
                    and len(row[0]) <= MAX_LABEL_LEN):
                out.append(f"{where}.rows[{i}] must be (label, cell), label at most {MAX_LABEL_LEN} characters")
                continue
            _cell(row[1], manifest, f"{where}.rows[{i}]", out)

    elif isinstance(w, Table):
        if not isinstance(w.columns, (tuple, list)):
            out.append(f"{where}: columns must be a tuple of strings, got {type(w.columns).__name__}")
            columns: list = []
        else:
            columns = list(w.columns)
            if not all(isinstance(c, str) for c in columns):
                out.append(f"{where}: columns must be strings")
            if len(columns) > MAX_COLUMNS:
                out.append(f"{where}: at most {MAX_COLUMNS} columns, got {len(columns)}")
        if isinstance(w.key, bool) or not isinstance(w.key, int) or not 0 <= w.key < max(1, len(columns)):
            out.append(f"{where}.key: must be a column index from 0 to {max(0, len(columns) - 1)}, got {w.key!r}")
        for i, row in enumerate(_rows_of(w.rows, where, "rows", out)):
            if not isinstance(row, tuple) or len(row) != len(columns):
                out.append(f"{where}.rows[{i}] must have one cell per column ({len(columns)} columns)")
                continue
            for j, cell in enumerate(row):
                _cell(cell, manifest, f"{where}.rows[{i}][{j}]", out)

    elif isinstance(w, Card):
        _title_field(w.title, MAX_LABEL_LEN, where, "title", out)
        if w.role is not None and w.role not in ROLES:
            out.append(f"{where}: role {w.role!r} must be one of {', '.join(ROLES)}")
        if w.href is not None:
            _url_field(w.href, where, "href", out)
        if w.layout not in CARD_LAYOUTS:
            out.append(f"{where}: layout {w.layout!r} must be one of None, 'grid'")
        if not isinstance(w.body, (tuple, list)):
            out.append(f"{where}.body: must be a tuple of widgets, got {type(w.body).__name__}")
        else:
            body = w.body
            if len(body) > MAX_BODY_ITEMS:
                out.append(f"{where}.body: at most {MAX_BODY_ITEMS} children, got {len(body)}")
                body = body[:MAX_BODY_ITEMS]
            for i, child in enumerate(body):
                if not isinstance(child, _BLOCKS):
                    out.append(f"{where}.body[{i}]: {type(child).__name__} is not a widget")
                    continue
                _check(child, manifest, f"{where}.body[{i}]", out, depth + 1, slot)


def _check_tile(widget: Tile, where: str, out: list[str]) -> None:
    _title_field(widget.label, MAX_LABEL_LEN, where, "label", out)
    if not _is_scalar(widget.value):
        out.append(f"{where}.value: must be a string, number or None, got {type(widget.value).__name__}")
    elif isinstance(widget.value, str) and len(widget.value) > MAX_VALUE_LEN:
        out.append(f"{where}.value: must be at most {MAX_VALUE_LEN} characters, got {len(widget.value)}")
    if widget.role not in ROLES:
        out.append(f"{where}: role {widget.role!r} must be one of {', '.join(ROLES)}")
    if widget.href is not None:
        _url_field(widget.href, where, "href", out)
    _str_field(widget.sub, MAX_TEXT_LEN, where, "sub", out)


def widget_problems(widget, *, slot: str, manifest) -> list[str]:
    out: list[str] = []
    name = type(widget).__name__
    try:
        if slot == "today.summary":
            if not isinstance(widget, Tile):
                return [f"{name}: today.summary takes only Tile widgets"]
            _check_tile(widget, name, out)
            return out
        if isinstance(widget, Tile):
            return ["Tile: only allowed in the today.summary slot"]
        if not isinstance(widget, _BLOCKS):
            return [f"{name}: not a widget (return orch.addons.widgets objects, never HTML or strings)"]
        _check(widget, manifest, name, out, 0, slot)
        return out
    except Exception as e:  # a widget must never crash a page render; report it as a problem instead
        return [f"{name}: could not be checked ({e})"]
