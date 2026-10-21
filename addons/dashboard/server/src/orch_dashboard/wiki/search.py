"""Title-first search, then one bounded search of committed blobs."""

import unicodedata

from ..errors import fail
from . import limits, reader


def search(scope, q: str) -> dict:
    # Reject controls even at the edges, before stripping ordinary whitespace.
    if any(unicodedata.category(c) == "Cc" for c in q):
        raise fail("invalid.input", "Use plain search text.", exit=5)
    q = q.strip()
    if not 1 <= len(q) <= limits.MAX_QUERY:
        raise fail("invalid.input", "Enter between 1 and 200 characters.", exit=5)
    if scope.repo is None:
        return {"pages": [], "cut": False}
    listed = reader.list_pages(scope.repo)
    pages = listed["pages"]
    hits = {
        r["slug"]: {**r, "snippet": reader.clean(r["title"], 160)}
        for r in pages
        if q.casefold() in r["title"].casefold() or q.casefold() in r["slug"].casefold()
    }
    by_slug = {r["slug"]: r for r in pages}
    # the content search reads exactly the pages that may be served, within the scan cap (see reader.scan_set)
    files, left_out = reader.scan_set(listed["sizes"], [r["slug"] for r in pages])
    found, searched_cut = reader.grep(scope.repo, q, files, limits.SEARCH_SECONDS)
    cut = listed["cut"] or left_out or searched_cut
    for slug, text in found:
        if slug in by_slug:
            hits.setdefault(slug, {**by_slug[slug], "snippet": reader.clean(text, 160)})
    return {"pages": list(hits.values())[: limits.SEARCH_HITS], "cut": cut or len(hits) > limits.SEARCH_HITS}
