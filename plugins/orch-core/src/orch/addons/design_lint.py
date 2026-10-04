"""Design warnings for `orch addon check` (design-system spec §4.5, W1-W18). They run on real widget output inside
the contract render (every slot x every health state) and never fail a check on their own: they print as △ and fail
only with `--strict`. Errors (E1-E3) stay in `widgets.widget_problems`. Text rules are heuristics on the widgets'
text fields; each warning starts with its rule id and names the place, e.g. "W4 Card.body[1]: ..."."""
from __future__ import annotations

import re

from orch.addons.widgets import KV, Action, Badge, Callout, Card, Chips, Link, Search, Table, Tabs, Text, Tile

PAGE_MAX_COLUMNS = 5  # page.* and board.external
SLIM_MAX_COLUMNS = 3  # ticket.* and today.from_addons
MAX_ROWS_WITHOUT_FILTER = 100
MAX_BADGE_WORDS = 3
MAX_BADGE_CHARS = 24
MAX_CHIPS = 8
MAX_CARD_ACTIONS = 3
MAX_TITLE_CHARS = 24
DEFAULT_EMPTY = "Nothing here."
ACRONYMS = frozenset({"PR", "PRS", "CI", "ID", "IDS", "URL", "API", "SQL", "DBR"})
_GLYPHS = "✓✕▲●◐○⚠✔❌"
# Extended_Pictographic, approximated with the blocks emoji live in (the stdlib has no property lookup).
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\U00002B50\U00002B55]")
_CAPS = re.compile(r"[A-Z]{2,}")
_FRESH = re.compile(r"\b(updated|ago|fresh|synced)\b", re.I)
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT|\bUTC\b")
_VAGUE_LINKS = frozenset({"here", "click here", "link", "more"})
_ROLE_WORDS = (  # (pattern, roles that may say it, why)
    (re.compile(r"\b(fail(ed|ing|s|ure|ures)?|errors?|broken)\b", re.I), ("err",), "failed, error and broken are err"),
    (re.compile(r"\b(stale|expired|login|warning)\b", re.I), ("warn", "err"), "stale, expired, login and warning are warn"),
    (re.compile(r"\b(running|in progress)\b", re.I), ("info",), "running and in progress are info"),
)
# "0 failed", "no errors", "nothing stale", "not running": the word is negated, so any role may say it.
_NEGATED = re.compile(r"\b(0|no|none|nothing|zero|without|not)\b\W+(\w+\W+)?(fail|error|broken|stale|expired|login|"
                      r"warning|running|in progress)", re.I)
# Product and company names a sentence-case title may capitalise (W18). Words with an inner capital (GitHub,
# DevOps) count as names too.
PROPER_NOUNS = frozenset({"Databricks", "Confluence", "Jira", "Slack", "Azure", "AWS", "Google", "Linear", "Notion",
                          "Claude", "Codex", "Copilot", "Teams", "Outlook", "Sentry", "Grafana", "Kubernetes", "Docker",
                          "Snowflake", "Terraform", "Unity", "Catalog", "Delta", "Spark", "Python",
                          "Mission", "Control", "Abacus", "Bitbucket", "Gitea", "Vercel", "Netlify", "Cloudflare"})
_CARD_ROLE_WORDS = {"warn": re.compile(r"\b(warn\w*|stale|login|expired|blocked|attention|partial)\b", re.I),
                    "err": re.compile(r"\b(fail\w*|errors?|broken)\b", re.I)}


def _words(text: str) -> int:
    """Words with a letter in them: "1 running 3 h" is two words (numbers and units read as one value)."""
    return sum(1 for w in text.split() if any(c.isalpha() for c in w))


def own_names(manifest) -> frozenset:
    """The addon's own name parts in capitals (an addon `acme-foo` may write its product name FOO): names, not
    shouting."""
    return frozenset(part.upper() for part in str(getattr(manifest, "name", "") or "").split("-") if len(part) >= 2)


def _caps(text: str, names: frozenset = frozenset()) -> str | None:
    """The first ALL CAPS word (2+ letters, not a known acronym, proper noun or the addon's own name); tokens with
    digits or dashes (ticket keys, GH-11) are names, not shouting."""
    for token in text.split():
        word = token.strip(".,;:!?()[]'\"")
        if _CAPS.fullmatch(word) and word not in ACRONYMS and word not in PROPER_NOUNS and word not in names:
            return word
    return None


def _strings(cell):
    if isinstance(cell, str):
        yield cell
    elif isinstance(cell, Text):
        yield cell.text


class _Lint:
    def __init__(self, slot: str, sample: str = "", names: frozenset = frozenset()):
        self.slot = slot
        self.sample = sample
        self.names = names
        self.out: list[str] = []

    def warn(self, rule: str, where: str, text: str) -> None:
        self.out.append(f"{rule} {where}: {text}")

    # ---- leaf rules, shared by blocks and cells ----

    def text(self, value: str, where: str, what: str) -> None:
        if not isinstance(value, str):
            return
        stripped = value.lstrip()
        if (stripped and stripped[0] in _GLYPHS) or _EMOJI.search(value):
            self.warn("W5", where, f"{what} starts with a status glyph or contains emoji; core adds the icon")
        if _TIMESTAMP.search(value):
            self.warn("W12", where, f"{what} looks like a formatted timestamp; return a Time widget")

    def title(self, value: str, where: str, what: str, *, bang: bool = True) -> None:
        if not isinstance(value, str):
            return
        if bang and "!" in value:
            self.warn("W6", where, f"{what} {value!r} has an exclamation mark")
        word = _caps(value, self.names)
        if word:
            self.warn("W7", where, f"{what} {value!r} has an ALL CAPS word ({word}); use sentence case")
        if value.rstrip().endswith(":"):
            self.warn("W7", where, f"{what} {value!r} ends with a colon")

    def badge(self, b: Badge, where: str) -> None:
        text = b.text if isinstance(b.text, str) else ""
        if self.sample:  # the contract's sample payload stands for one short value, not a sentence
            text = text.replace(self.sample, "x")
        if _words(text) > MAX_BADGE_WORDS or len(text) > MAX_BADGE_CHARS:
            self.warn("W4", where, f"Badge {text!r} is longer than {MAX_BADGE_WORDS} words or {MAX_BADGE_CHARS} "
                                   "characters; status text is short (use a Callout for sentences)")
        self.text(text, where, "Badge")
        self.title(text, where, "Badge")
        if b.role == "ok" and _FRESH.search(text):
            self.warn("W8", where, f"Badge {text!r} shows freshness; core's health line does that")
        self.role_words(b.role, text, where, "Badge")

    def role_words(self, role: str, text: str, where: str, what: str) -> None:
        if _NEGATED.search(text):
            return
        for pattern, roles, why in _ROLE_WORDS:
            if pattern.search(text) and role not in roles:
                self.warn("W9", where, f"{what} role {role!r} does not match its words {text!r} ({why})")
                return

    def link(self, link: Link, where: str) -> None:
        text = link.text.strip().lower() if isinstance(link.text, str) else ""
        if text in _VAGUE_LINKS or (isinstance(link.text, str) and link.text.strip() == link.url):
            self.warn("W13", where, f"Link text {link.text!r} does not say where it goes")

    def cell(self, cell, where: str) -> None:
        if isinstance(cell, Badge):
            self.badge(cell, where)
        elif isinstance(cell, Link):
            self.link(cell, where)
        elif isinstance(cell, Action):
            self.title(cell.label, where, "Action label")
        for s in _strings(cell):
            self.text(s, where, "Text" if isinstance(cell, Text) else "Cell")

    # ---- blocks ----

    def table(self, t: Table, where: str, filtered: bool) -> None:
        columns = list(t.columns) if isinstance(t.columns, (tuple, list)) else []
        rows = list(t.rows) if isinstance(t.rows, (tuple, list)) else []
        limit = PAGE_MAX_COLUMNS if self.slot.startswith("page.") or self.slot == "board.external" else SLIM_MAX_COLUMNS
        if len(columns) > limit:
            self.warn("W1", where, f"Table has {len(columns)} columns; at most {limit} fit in {self.slot}")
        if self.slot.startswith("page.") and len(rows) > MAX_ROWS_WITHOUT_FILTER and not filtered:
            self.warn("W2", where, f"Table has {len(rows)} rows and the page has no Search or Chips to filter them")
        empty = t.empty if isinstance(t.empty, str) else ""
        if empty.strip() == DEFAULT_EMPTY or _words(empty) < 4:
            self.warn("W3", where, f"Table empty text {empty!r} should say why it is empty and what next")
        for c in columns:
            self.title(c, where, "Column", bang=False)
        for i, row in enumerate(rows):
            if not isinstance(row, tuple):
                continue
            if sum(1 for c in row if isinstance(c, Action)) > 1:
                self.warn("W14", f"{where}.rows[{i}]", "more than one Action in a table row; make the lesser one "
                                                       "a quiet Action elsewhere")
            for j, c in enumerate(row):
                self.cell(c, f"{where}.rows[{i}][{j}]")

    def block(self, w, where: str, depth: int, filtered: bool) -> None:
        if isinstance(w, Card):
            self.card(w, where, depth, filtered)
        elif isinstance(w, Table):
            self.table(w, where, filtered)
        elif isinstance(w, Callout):
            self.title(w.title, where, "Callout title")
            self.text(w.text, where, "Callout text")
            if isinstance(w.title, str):
                self.role_words(w.role, w.title, where, "Callout")
        elif isinstance(w, Chips):
            items = list(w.items) if isinstance(w.items, (tuple, list)) else []
            if len(items) > MAX_CHIPS:
                self.warn("W11", where, f"Chips has {len(items)} items; at most {MAX_CHIPS} (use Search for more)")
            for i, item in enumerate(items):
                self.cell(item, f"{where}.items[{i}]")
        elif isinstance(w, Tabs):
            for i, item in enumerate(w.items if isinstance(w.items, (tuple, list)) else ()):
                self.cell(item, f"{where}.items[{i}]")
        elif isinstance(w, KV):
            for i, row in enumerate(w.rows if isinstance(w.rows, (tuple, list)) else ()):
                if isinstance(row, tuple) and len(row) == 2:
                    self.cell(row[1], f"{where}.rows[{i}]")
        else:
            self.cell(w, where)

    def sequence(self, items: list, where: str, depth: int, filtered: bool, *, top: bool) -> None:
        callouts = [i for i, w in enumerate(items) if isinstance(w, Callout)]
        if top and self.slot.startswith("page.") and len(callouts) > 1:
            self.warn("W10", where, f"{len(callouts)} Callouts at the top level; one per page, at the top")
        first_table = next((i for i, w in enumerate(items) if isinstance(w, Table)), None)
        if first_table is not None and any(i > first_table for i in callouts):
            self.warn("W10", where, "a Callout after a Table; put it before the table it explains")
        chips = [w for w in items if isinstance(w, Chips)]
        if len(chips) > 1 and any(not (isinstance(c.label, str) and c.label.strip()) for c in chips):
            self.warn("W11", where, "two or more Chips rows without a label each")
        for i, w in enumerate(items):
            self.block(w, type(w).__name__ if top else f"{where}[{i}]", depth, filtered)

    def card(self, c: Card, where: str, depth: int, filtered: bool) -> None:
        self.title(c.title, where, "Card title")
        body = list(c.body) if isinstance(c.body, (tuple, list)) else []
        if sum(1 for w in body if isinstance(w, Action)) > MAX_CARD_ACTIONS:
            self.warn("W14", where, f"Card has more than {MAX_CARD_ACTIONS} Actions")
        if depth >= 1 and any(isinstance(w, Table) for w in body):
            self.warn("W15", where, "a Table inside a nested Card draws nested borders; use one table with the "
                                    "things as rows, or a top-level Card")
        if c.role in _CARD_ROLE_WORDS and not _has_badge(body) \
                and not (isinstance(c.title, str) and _CARD_ROLE_WORDS[c.role].search(c.title)):
            self.warn("W16", where, f"Card(role={c.role!r}) says it only with colour; add a Badge or the role word "
                                    "to the title")
        self.sequence(body, f"{where}.body", depth + 1, filtered, top=False)

    def tile(self, t: Tile, where: str) -> None:
        if not (isinstance(t.sub, str) and t.sub.strip()):
            self.warn("W17", where, "Tile has no sub-line (say what the number counts or where)")
        if isinstance(t.value, str):
            try:
                float(t.value.replace("'", "").replace(",", ""))
            except ValueError:
                self.warn("W17", where, f"Tile value {t.value!r} is not a number (None reads as unknown)")
        self.title(t.label, where, "Tile label")


def _has_badge(items) -> bool:
    for w in items:
        if isinstance(w, Badge):
            return True
        if isinstance(w, Card) and _has_badge(w.body if isinstance(w.body, (tuple, list)) else ()):
            return True
        if isinstance(w, Table) and any(isinstance(c, Badge) for row in (w.rows or ()) if isinstance(row, tuple) for c in row):
            return True
        if isinstance(w, KV) and any(isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], Badge) for r in (w.rows or ())):
            return True
        if isinstance(w, Chips) and any(isinstance(i, Badge) for i in (w.items or ())):
            return True
    return False


def _filtered(items) -> bool:
    for w in items:
        if isinstance(w, (Search, Chips)):
            return True
        if isinstance(w, Card) and _filtered(w.body if isinstance(w.body, (tuple, list)) else ()):
            return True
    return False


def widget_warnings(widgets: list, slot: str, *, sample: str = "", names: frozenset = frozenset()) -> list[str]:
    """Design warnings for one render of `slot` (the widgets an addon returned, already free of errors).
    `sample` is the contract's sample payload: where an addon echoes data into a Badge, it counts as one word.
    `names` (`own_names(manifest)`) are the addon's own name parts, allowed in capitals."""
    lint = _Lint(slot, sample, names)
    if slot == "today.summary":
        for i, w in enumerate(widgets):
            if isinstance(w, Tile):
                lint.tile(w, f"Tile[{i}]")
        return list(dict.fromkeys(lint.out))
    try:
        lint.sequence(list(widgets), slot, 0, _filtered(widgets), top=True)
    except Exception as e:  # a lint must never crash the check; widget_problems already reported real errors
        lint.out.append(f"W0 {slot}: could not be linted ({type(e).__name__})")
    return list(dict.fromkeys(lint.out))


def manifest_warnings(manifest) -> list[str]:
    out = []
    titles = [("title", manifest.title)] + ([("menu.title", manifest.menu["title"])] if manifest.menu else [])
    for field, value in titles:
        if not isinstance(value, str):
            continue
        if len(value) > MAX_TITLE_CHARS:
            out.append(f"W18 manifest {field}: {value!r} is longer than {MAX_TITLE_CHARS} characters")
        words = [w.strip(".,;:()'\"") for w in value.split()]
        title_case = [w for w in words[1:] if w[:1].isupper() and w[1:].islower() and w not in PROPER_NOUNS]
        if title_case or _caps(value, own_names(manifest)):
            out.append(f"W18 manifest {field}: {value!r} is not sentence case")
    return out
