"""The one fenced-code rule (CommonMark §4.5) every line-based reader of ticket text uses: the section splitter,
`neutral_text`, the body-file parser, the evidence parser and the widget parser. Before this module each kept its
own `^\\s{0,3}(```|~~~)` and disagreed with markdown-it (```` opened, ``` closed it in orch but not on the page),
so a heading inside a fence could become a section in the file while the page showed it as code.

Opening: up to 3 spaces, a run of 3+ backticks or tildes, an info string (a backtick fence's may not contain a
backtick). Closing: up to 3 spaces, a run of the same character at least as long, nothing but whitespace after."""
from __future__ import annotations

import re

_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# Anything that could be read as a fence line by a looser reader: what `neutral_text` escapes (a backslash costs
# nothing, a missed fence forges a section).
_LOOSE = re.compile(r"^\s{0,3}(```|~~~)")


def opening(line: str) -> str | None:
    """The fence run `line` opens (e.g. "````"), or None."""
    m = _OPEN.match(line)
    if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
        return None
    return m.group(1)


def info(line: str) -> str:
    """The info string of an opening fence line ("" when there is none)."""
    m = _OPEN.match(line)
    return m.group(2).strip() if m else ""


def closes(line: str, run: str) -> bool:
    """Whether `line` closes a fence opened with `run`."""
    m = _OPEN.match(line)
    return bool(m and m.group(1)[0] == run[0] and len(m.group(1)) >= len(run) and not m.group(2).strip())


def step(fence: str | None, line: str) -> tuple[str | None, bool]:
    """(the fence open after `line`, whether `line` itself is a fence line). `fence` is the run open before it."""
    if fence is None:
        run = opening(line)
        return run, run is not None
    return (None, True) if closes(line, fence) else (fence, False)


def could_fence(line: str) -> bool:
    """True for any line a reader might take for a fence line (looser than `opening`, on purpose)."""
    return bool(_LOOSE.match(line))
