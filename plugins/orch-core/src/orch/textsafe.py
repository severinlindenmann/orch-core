"""Ticket text shown to the human for a decision, made unable to hide or rewrite what is on screen.

A character is hidden when its Unicode category is a control (Cc), format (Cf: zero-width, bidi marks and
overrides, soft hyphen, tag characters, ...), line or paragraph separator (Zl, Zp), private use (Co) or unassigned
(Cn), tab and newline aside, plus the invisible fillers U+034F, U+115F, U+1160, U+3164 and U+FFA0. Such characters
can move the cursor, erase a line, reorder or hide text while the human reads it. `visible` and `lines` turn each
into a visible escape (`\\x1b`, `<U+202E>`); `has_hidden` tells whether text holds any; `strip_hidden` removes them
(for file names); `html_badges` escapes text for HTML with each hidden character as a visible badge.
"""
from __future__ import annotations

import re
import unicodedata
from html import escape as _html_escape

_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})
_EXTRA = frozenset("͏ᅟᅠㅤﾠ")
_ALLOWED = frozenset("\t\n")


def is_hidden(c: str) -> bool:
    return c not in _ALLOWED and (c in _EXTRA or unicodedata.category(c) in _CATEGORIES)


def _escape(c: str) -> str:
    return f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"<U+{ord(c):04X}>"


_ASCII_HIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f]")


def has_hidden(text) -> bool:
    s = str(text or "")
    if s.isascii():  # the common case, without a per-character category lookup
        return bool(_ASCII_HIDDEN.search(s))
    return any(is_hidden(c) for c in s)


def decodes_to_hidden(text) -> bool:
    """`text` holds hidden characters itself or once decoded the way it is shown: HTML character references
    (`&#x202E;`, `&#8238;`, `&ZeroWidthSpace;`, `&shy;`), percent-encoding (autolinks), and the dashboard's Markdown
    rendering when it is installed."""
    from html import unescape
    from urllib.parse import unquote
    s = str(text or "")
    if has_hidden(s):
        return True
    forms = [unescape(s), unquote(s, errors="replace"), unquote(unescape(s), errors="replace")]
    try:
        from orch.dashboard.markdown import _md
    except ImportError:  # the dashboard extra is not installed: the decodings above cover what it would show
        pass
    else:
        forms.append(unescape(_md.render(s)))
    return any(has_hidden(f) for f in forms)


def html_text(text) -> str:
    """`text` escaped for HTML text or attribute values, hidden characters as plain visible `<U+XXXX>` escapes."""
    return "".join(_html_escape(f"<U+{ord(c):04X}>") if is_hidden(c) else _html_escape(c, quote=True)
                   for c in str(text or ""))


def visible(text) -> str:
    """One field as safe display text: hidden characters escaped (newlines too, for single-line fields)."""
    return "".join(_escape(c) if is_hidden(c) else c for c in str(text or "")).replace("\n", "\\n")


def lines(text) -> list[str]:
    """Multi-line text as safe display lines: split on newlines only, every other hidden character escaped."""
    return ["".join(_escape(c) if is_hidden(c) else c for c in line) for line in str(text or "").split("\n")]


def strip_hidden(text, *, keep_whitespace: bool = True) -> str:
    """`text` without hidden characters (and without tab and newline unless `keep_whitespace`)."""
    return "".join(c for c in str(text or "")
                   if not is_hidden(c) and (keep_whitespace or c not in _ALLOWED))


def badge(c: str) -> str:
    return f'<span class="hidden-char" title="hidden character">&lt;U+{ord(c):04X}&gt;</span>'


def html_badges(text) -> str:
    """`text` escaped for HTML, every hidden character replaced by a visible badge (never the raw code point)."""
    return "".join(badge(c) if is_hidden(c) else _html_escape(c, quote=True) for c in str(text or ""))


def badge_html(html: str) -> str:
    """Rendered HTML (e.g. from Markdown) with hidden characters in text nodes shown as badges and removed from tags
    and attributes."""
    out, i, n = [], 0, len(html)
    while i < n:
        if html[i] == "<":
            end = html.find(">", i)
            end = n if end == -1 else end + 1
            out.append(strip_hidden(html[i:end]))
            i = end
            continue
        j = html.find("<", i)
        j = n if j == -1 else j
        out.append("".join(badge(c) if is_hidden(c) else c for c in html[i:j]))
        i = j
    return "".join(out)
