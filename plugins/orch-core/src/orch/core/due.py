"""The optional due date of a ticket (#174): `due: YYYY-MM-DD` in the frontmatter, set with `orch new --due` and
`orch due`. Planning only: no gate binds it and nothing reminds anyone. A ticket that is not done and due within
DUE_SOON_DAYS days (or overdue) is "soon" (or "overdue"); `orch next` puts those first within their priority.
"Today" is the local date of `orch.clock.now()`; every function takes `today` so tests pass a fixed one."""
from __future__ import annotations

import re
from datetime import date

from orch.core.constants import DUE_SOON_DAYS
from orch.errors import UsageError

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_due(value) -> date | None:
    """The date `value` names when it is a valid `YYYY-MM-DD` string, else None."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def due_problem(value) -> str | None:
    """Why a frontmatter `due` value is not valid, or None (absent and null are valid: no due date)."""
    if value is None:
        return None
    if parse_due(value) is None:
        return f"due {value!r} is not a date in the form YYYY-MM-DD"
    return None


def checked_due(text: str) -> str:
    """`text` as a due date to store, or UsageError."""
    text = str(text).strip()
    if parse_due(text) is None:
        raise UsageError(f"due date {text!r} is not a valid date", hint="use YYYY-MM-DD, e.g. 2026-10-31")
    return text


def today() -> date:
    from orch.clock import now
    return now().astimezone().date()


def due_state(meta: dict, status: str, today_: date | None = None) -> str | None:
    """"overdue", "soon" (due within DUE_SOON_DAYS days, today included) or None: no valid due date, not soon,
    or the ticket is done."""
    due = parse_due((meta or {}).get("due"))
    if due is None or status == "done":
        return None
    days = (due - (today_ or today())).days
    if days < 0:
        return "overdue"
    return "soon" if days <= DUE_SOON_DAYS else None
