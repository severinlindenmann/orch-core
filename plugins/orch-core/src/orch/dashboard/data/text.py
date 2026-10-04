"""Text helpers shared by the Markdown exports (Timeline, Reports)."""
from __future__ import annotations

import re

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MD_ESCAPE_RE = re.compile(r"[\[\]()<>]")


def clean(text) -> str:
    """Strip control characters and collapse all whitespace (incl. newlines) to single spaces,
    so user-supplied text can't break a table row or inject a Markdown heading/bullet."""
    text = _CONTROL_RE.sub("", str(text))
    return " ".join(text.split())


def md_escape(text) -> str:
    """`clean`, plus backslash-escaping Markdown link/angle-bracket syntax, so user text (a log
    line, a hand-edited ticket field) can't turn into a link or look like an HTML tag in an export."""
    return _MD_ESCAPE_RE.sub(lambda m: "\\" + m.group(0), clean(text))
