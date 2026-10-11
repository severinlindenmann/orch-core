"""The text of the session-start and pre-compact hooks (ticket-format §10.2): who the agent works for, its grant, its
claim, what has not been read yet and what to do next. At most six lines, built from what ``orch status`` knows.

These are pure functions over plain values, so the budget is tested without a workspace. The operation
``instructions.hook`` (``orch instructions hook session-start``) gathers the values and calls them.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "PRE_COMPACT_MAX_LINES",
    "SESSION_START_MAX_LINES",
    "STALE_LINE",
    "pre_compact_lines",
    "session_start_lines",
]

SESSION_START_MAX_LINES = 6
PRE_COMPACT_MAX_LINES = 4
STALE_LINE = "instructions stale: run orch instructions sync"
_UNREAD_SHOWN = 2


def session_start_lines(
    *,
    person: str,
    grant: str | None,
    claim: str | None,
    claim_line: str | None,
    unread: Sequence[str],
    stale: bool,
    next_hint: str,
) -> list[str]:
    """The lines (the first is the ``ok`` line the CLI prints). ``claim_line`` is the status line of the claim
    (``DEMO-0043 in-progress (your claim) · T3 next · 2 new events``), ``unread`` the undelivered decisions
    (``DEMO-0043 #4 answered Q1``), only the first few are shown. Never more than six lines."""
    head = f"ok session-start {person}" + (f" · grant {grant}" if grant else " · no grant")
    out = [head]
    if not grant:
        out.append("no grant: only ask, log, artifact add (no --ac) work; the person runs orch grant")
    if claim_line:
        out.append(claim_line)
    elif claim is None:
        out.append("no claim")
    if unread:
        shown = list(unread[:_UNREAD_SHOWN])
        more = len(unread) - len(shown)
        out.append("unread: " + "; ".join(shown) + (f"; +{more} more" if more > 0 else "") + " (orch wait)")
    if stale:
        out.append(STALE_LINE)
    out.append(f"next: {next_hint}")
    assert len(out) <= SESSION_START_MAX_LINES  # head, no-grant, claim, unread, stale, next: six at most
    return out


def pre_compact_lines(*, claim: str | None, open_questions: int) -> list[str]:
    """What to keep when the context is summarised (the first line is the ``ok`` line, the last the ``next:`` line)."""
    out = [
        "ok pre-compact \u00b7 keep in the summary: your claim, the current task, open questions, decisions handed over"
    ]
    out.append(f"your claim: {claim}" if claim else "you hold no claim")
    if open_questions:
        out.append(f"{open_questions} question(s) still open")
    out.append("next: orch status, then orch show")
    return out
