"""Quick tasks (H11f, #308, D-P2-35): a quick task is an ordinary ticket, ``type: chore`` with the label ``quick``.

There is no quick-task store and no quick-task event. This module holds what both modes share for the one read,
``quick.list``: the label, the default size limits, the counts taken from a ``diff`` result, ``over`` and the text.
The limits (``max_commits`` 1, ``max_files`` 3 by default) are **display only**: nothing refuses a quick task that
outgrows them, it shows ``over`` and the person can make it an ordinary ticket by removing the label.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "DEFAULT_MAX_COMMITS",
    "DEFAULT_MAX_FILES",
    "BUDGET_S",
    "LABEL",
    "ADDON",
    "is_on",
    "require_on",
    "TITLE_MAX",
    "counts",
    "lines",
    "over",
]

LABEL = "quick"
TITLE_MAX = 120  # a quick task's title: one line, at most this many characters
DEFAULT_MAX_COMMITS = 1
DEFAULT_MAX_FILES = 3
#: seconds for the reader calls of all rows of one list together (both modes); later rows have unknown counts
BUDGET_S = 20.0


ADDON = "quick"
MESSAGE = "Quick tasks is switched off in this workspace; an owner can turn it on in Add-ons"


def is_on(addons: Any) -> bool:
    """Is the built-in ``quick`` add-on granted and enabled? No grant means Off (there is no legacy default)."""
    a = (addons or {}).get(ADDON)
    if a is None:
        return False
    get = a.get if isinstance(a, Mapping) else (lambda k, d=None: getattr(a, k, d))
    return bool(get("enabled", False)) and not get("purged", False)


def require_on(addons: Any) -> None:
    from orch.ops.errors import OrchError

    if not is_on(addons):
        raise OrchError("quick.off", MESSAGE)


def counts(diff: dict[str, Any]) -> tuple[int, int]:
    """(commits, files) of a ``diff`` result: every commit of ``base..head`` in each linked repo (the ones the reader
    lists and ``commits_more``), and the changed files."""
    commits = sum(len(s["commits"]) + int(s["commits_more"]) for s in diff["sources"])
    return commits, len(diff["files"])


def over(commits: int | None, files: int | None, max_commits: int, max_files: int) -> bool | None:
    """``None`` when the counts are unknown (no repo or branch linked, or the reader could not read it)."""
    if commits is None or files is None:
        return None
    return commits > max_commits or files > max_files


def lines(rows: list[dict[str, Any]], total: int, next_page: str | None) -> tuple[list[str], list[str]]:
    """The text lines and the hints of a ``quick.list`` answer (both modes render the same rows the same way)."""
    from orch.ops.views import fence

    body = "\n".join(_line(r) for r in rows)
    out = fence(body, "quick tasks") if rows else ["no quick tasks"]
    if next_page is not None:
        out.append(f"{total} in all; more with --page {next_page}")
    hints = [f"orch show {rows[0]['key']}" if rows else 'orch quick add "TITLE"']
    return out, hints


def _line(r: dict[str, Any]) -> str:
    from orch.ops.views import short

    q = r["quick"]
    size = "?" if q["commits"] is None else f"{q['commits']}c/{q['files']}f"
    flag = " over" if q["over"] else ""
    return f"{r['key']} {r['status']} {size}{flag}: {short(r['title'], 80)}"
