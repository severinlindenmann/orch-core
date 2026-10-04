"""Ticket keys in addon widget text become links (design-system spec §3.0): the text is escaped first, then
every key of this workspace's prefix is wrapped. Keys of other systems (GH-1, PR #2) stay plain text."""
from __future__ import annotations

import re
from functools import lru_cache

from markupsafe import Markup, escape


@lru_cache(maxsize=16)
def _pattern(prefix: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-])({re.escape(prefix)}-\d+)(?![\w-])", re.IGNORECASE)


def link_keys(text, prefix) -> Markup:
    from orch.textsafe import has_hidden, html_text
    raw = "" if text is None else str(text)
    # escaped first; hidden characters (bidi, zero-width, ...) become visible <U+XXXX> like every template value
    safe = Markup(html_text(raw)) if has_hidden(raw) else escape(raw)
    if not isinstance(prefix, str) or not prefix:
        return safe
    return Markup(_pattern(prefix).sub(lambda m: f'<a class="lnk key" href="/t/{m.group(1).upper()}">{m.group(1)}</a>',
                                       str(safe)))
