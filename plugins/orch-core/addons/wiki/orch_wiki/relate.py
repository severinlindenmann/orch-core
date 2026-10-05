"""Related pages, docs that may need an update, and search (spec A1 §7.4, ruling R13). Pure functions over page
items, branch-diff items and the index texts. Cached values are untrusted: anything that is not the expected type
is ignored."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .globs import glob_match, normalize
from .pages import KEY, WORD

STATUSES = ("testing", "done")
COMMON_MIN_PAGES = 4  # fewer pages than this and "on most of them" says nothing
COMMON_SHARE = 0.5  # a term on more than this share of the pages is no signal
MAX_SCANS = 20  # page texts read with a regex per related_pages call; the rest is looked up
TARGET = re.compile(r"[A-Z][A-Z0-9]*-\d+\|[^|\n]{1,201}\|[^|\n]{1,200}")


def _strs(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


@dataclass(frozen=True)
class Hint:
    ticket: str
    space: str
    page_id: str
    title: str
    url: str
    reason: str

    @property
    def key(self) -> str:
        return f"{self.ticket}|{self.space}|{self.page_id}"


def _matches(page: dict, changed) -> list[str]:
    globs = _strs(page.get("documents"))
    links = {normalize(link) for link in _strs(page.get("file_links"))}
    hits = [path for repo, path in changed
            if normalize(path) in links or any(glob_match(g, path, repo) for g in globs)]
    return list(dict.fromkeys(hits))


def stale_docs(diff_items, pages, statuses: dict, dismissed=frozenset()) -> list[Hint]:
    """Pages whose documents: globs or file links match files a testing/done ticket's branch changed."""
    changed_by_ticket: dict[str, list[tuple[str | None, str]]] = {}
    for d in diff_items:
        if not isinstance(d, dict):
            continue
        ticket = str(d.get("ticket") or "")
        if statuses.get(ticket) not in STATUSES:
            continue
        repo = d.get("repo") if isinstance(d.get("repo"), str) else None
        changed_by_ticket.setdefault(ticket, []).extend((repo, f) for f in _strs(d.get("files")))
    hints = []
    for ticket, changed in sorted(changed_by_ticket.items()):
        for page in pages:
            hits = _matches(page, changed)
            if not hits:
                continue
            sample = ", ".join(hits[:3]) + (f" and {len(hits) - 3} more" if len(hits) > 3 else "")
            hint = Hint(ticket, str(page.get("space") or ""), str(page.get("id") or ""),
                        str(page.get("title") or page.get("id") or "page"), str(page.get("url") or ""),
                        f"{len(hits)} changed file(s) match: {sample}"[:500])
            if hint.key not in dismissed:
                hints.append(hint)
    return hints


def _pattern(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


def ticket_terms(ticket) -> list[tuple[str, re.Pattern]]:
    meta = ticket.meta or {}
    terms = [ticket.id]
    prefix, _, number = ticket.id.partition("-")
    if number.isdigit():
        terms.append(f"{prefix}-{int(number)}")
    terms += [str(x["key"]) for x in meta.get("external") or [] if isinstance(x, dict) and x.get("key")]
    terms += [label for label in _strs(meta.get("labels")) if len(label) >= 3]
    terms += [repo for repo in _strs(meta.get("repos")) if len(repo) >= 3]
    branches = meta.get("branches")
    terms += [repo for repo in (branches if isinstance(branches, dict) else {}) if isinstance(repo, str) and len(repo) >= 3]
    return [(term, _pattern(term)) for term in list(dict.fromkeys(t for t in terms if t))[:20]]


def _no_mentions(page) -> None:
    return None


def related_pages(ticket, pages, text_of, mentions_of=_no_mentions, limit: int = 5) -> list[tuple[dict, str]]:
    """Pages that mention the ticket key, its external keys, labels or repo names, most mentions first.
    mentions_of(page) gives the counts made at fetch time, so a render only looks them up. A term that is neither
    one word nor a key (a label such as data-model), or a page without counts (an older index), is matched with
    the regex on the page text, on at most MAX_SCANS texts per call."""
    terms = ticket_terms(ticket)
    scored = []
    per_page = []
    scans = 0
    for page in pages:
        found = mentions_of(page)
        found = found if isinstance(found, dict) else None
        hay = None
        counts = []
        for term, pattern in terms:
            key = term.lower()
            if found is not None and (KEY.fullmatch(key) or WORD.fullmatch(key)):
                counts.append((term, found.get(key, 0)))
                continue
            if found is not None and not all(found.get(w) for w in WORD.findall(key)):
                counts.append((term, 0))  # a word of the term is not on the page, so the term is not either
                continue
            if hay is None:
                if scans >= MAX_SCANS:
                    counts.append((term, 0))
                    continue
                scans += 1
                hay = f"{str(page.get('title') or '').lower()}\n{text_of(page)}"
            counts.append((term, len(pattern.findall(hay))))
        per_page.append((page, counts))
    # A term on most pages (the harness repo's name, a label every page carries) says nothing about this ticket: it is
    # left out, unless it is a ticket key or an external key, which always mean something. Needs a few pages to judge.
    common = set()
    if len(per_page) >= COMMON_MIN_PAGES:
        for term, _ in terms:
            if not KEY.fullmatch(term.lower()):
                hits = sum(1 for _, counts in per_page if any(t == term and n for t, n in counts))
                if hits / len(per_page) > COMMON_SHARE:
                    common.add(term)
    for page, counts in per_page:
        counts = [(t, n) for t, n in counts if t not in common]
        total = sum(n for _, n in counts)
        if not total and ticket.id in _strs(page.get("links")):
            counts, total = [(ticket.id, 1)], 1
        if total:
            why = ", ".join(term if n == 1 else f"{term} ×{n}" for term, n in counts if n)
            scored.append((total, str(page.get("updated_at") or ""), page, f"mentions {why}"[:200]))
    scored.sort(key=lambda s: s[1], reverse=True)
    scored.sort(key=lambda s: s[0], reverse=True)
    return [(page, why) for _, _, page, why in scored[:limit]]


QUOTES = "\"'`\u201c\u201d\u2018\u2019\u201e\u201a\u00ab\u00bb"  # straight and typographic quotes around a word or phrase


def search_words(query: str) -> list[str]:
    """The words of a query: lowercased, split on whitespace, quotes around a word dropped ("dbt" finds dbt), at most 5."""
    return [w for w in (x.strip(QUOTES) for x in str(query).lower().split()) if w][:5]


def search(pages, text_of, query: str, limit: int = 50) -> list[dict]:
    """In-memory search over cached pages: every word must be in the title or the text."""
    words = search_words(query)
    if not words:
        return []
    found = []
    for page in pages:
        title = str(page.get("title") or "").lower()
        text = text_of(page)
        if all(w in title or w in text for w in words):
            found.append((sum(title.count(w) * 5 + text.count(w) for w in words), page))
    found.sort(key=lambda s: s[0], reverse=True)
    return [page for _, page in found[:limit]]
