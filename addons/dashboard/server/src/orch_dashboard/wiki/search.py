"""Title-first search, then one bounded search of committed blobs."""

import os
import unicodedata

from orch import gitbrowse, gitsafe

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
    eligible = {"wiki/" + r["slug"] + ".md": r for r in pages if not r["too_large"]}
    cut = listed["cut"]
    if sum(listed["sizes"][r["slug"]] for r in eligible.values()) > limits.SEARCH_SCAN_BYTES:
        cut = True
    else:
        try:
            result = gitbrowse.run(
                scope.repo.folder,
                "tree_grep",
                os.environ,
                operand=(q, "HEAD", "wiki"),
                timeout=limits.SEARCH_SECONDS,
                max_bytes=1_000_000,
            )
        except gitsafe.Unsafe:
            raise fail("invalid.input", "Use plain search text.", exit=5) from None
        cut = cut or result.cut or result.timed_out or result.code not in (0, 1)
        for path, _line, text in gitbrowse.parse_grep(result.out):
            row = eligible.get(path.partition(":")[2])
            if row:
                hits.setdefault(row["slug"], {**row, "snippet": reader.clean(text, 160)})
    return {"pages": list(hits.values())[: limits.SEARCH_HITS], "cut": cut or len(hits) > limits.SEARCH_HITS}
