"""Host session records (H6b, §0.8 ``session.list``, §0.8.5 ``grant.list`` ``session_ids``): placeholder, filled by the
implementer. :func:`record` is called by the dispatcher for every request it serves, after the column."""

from __future__ import annotations

from typing import Any


def record(call: Any) -> None:
    """Note the (grant, session) of a column-G request with ``auth.session``. Never raises into dispatch."""
    return None
