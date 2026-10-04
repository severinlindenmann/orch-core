"""The `pages` provider interface (spec A1 §5.3, §7.4). A pages provider is an ordinary provider with kind "pages"
whose items are PageItems. It may also write a search index (page text) in the addon's state folder; widgets read
it during renders (a file read, cached by mtime), so snapshots stay small."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Protocol

PAGE_PROVIDERS = ("github-wiki", "confluence", "local")
MAX_INDEX_CHARS = 50_000
# Mentions, counted at fetch time so a render only looks them up (related pages): every word of three or more
# letters and digits, and every key such as demo-0003 or gh-12, both bounded by anything that is not [a-z0-9].
WORD = re.compile(r"[a-z0-9]{3,}")
KEY = re.compile(r"(?<![a-z0-9])[a-z][a-z0-9]*-\d+(?![a-z0-9])")
_RUN = re.compile(r"[a-z0-9]+")


class PagesProvider(Protocol):
    id: str
    kind: str  # "pages"
    interval_s: int

    def scopes(self, ctx) -> list[str]: ...

    def fetch(self, ctx, scope: str, previous): ...


def page_item(*, provider: str, space: str, id: str, title: str, url: str, path: str, updated_at: str,
              author: str | None = None, excerpt: str = "", links=(), documents=(), file_links=()) -> dict:
    item = {"provider": provider, "space": space, "id": id[:200], "title": title[:200], "url": url, "path": path,
            "updated_at": updated_at, "excerpt": excerpt[:300], "links": sorted(set(links)),
            "documents": list(documents)[:50], "file_links": sorted(set(file_links))[:200]}
    if author:
        item["author"] = str(author)[:100]
    return item


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:60] + "-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def index_path(state_dir, provider: str, space: str) -> Path:
    return Path(state_dir) / "index" / f"{provider}.{_slug(space)}.json"


def mentions(title: str, text: str) -> dict[str, int]:
    """{word or key: count} over the page title and its (lowercased, capped) index text."""
    hay = f"{str(title).lower()}\n{str(text)[:MAX_INDEX_CHARS].lower()}"
    out = dict(Counter(m for m in _RUN.findall(hay) if len(m) >= 3))
    out.update(Counter(KEY.findall(hay)))
    return out


def write_index(state_dir, provider: str, space: str, texts: dict, titles=None) -> None:
    path = index_path(state_dir, provider, space)
    path.parent.mkdir(parents=True, exist_ok=True)
    pages = {str(k): str(v)[:MAX_INDEX_CHARS].lower() for k, v in texts.items()}
    data = {"schema": 1, "pages": pages,
            "mentions": {k: mentions(str((titles or {}).get(k) or ""), v) for k, v in pages.items()}}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _counts(value) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {str(k): v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0}


class IndexReader:
    """Page texts (for search) and mentions (for related pages), re-read only when the index file changes."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self._cache: dict[Path, tuple[float, dict, dict]] = {}

    def _load(self, provider: str, space: str) -> tuple[dict, dict]:
        path = index_path(self.state_dir, provider, space)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}, {}
        hit = self._cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1], hit[2]
        texts, found = {}, {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            pages = data.get("pages")
            texts = {str(k): str(v) for k, v in pages.items()} if isinstance(pages, dict) else {}
            raw = data.get("mentions")
            for k, v in (raw.items() if isinstance(raw, dict) else ()):
                counts = _counts(v)
                if counts is not None:
                    found[str(k)] = counts
        self._cache[path] = (mtime, texts, found)
        return texts, found

    def texts(self, provider: str, space: str) -> dict:
        return self._load(provider, space)[0]

    def mentions(self, provider: str, space: str) -> dict:
        """{page id: {word or key: count}}; a page missing here (an index from an older version) has none."""
        return self._load(provider, space)[1]
