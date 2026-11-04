"""The one phrasing of an add-on's wake events for every review a person reads (addon-api 1.2, #718): the signing
prompt, ``orch addon grant``, the hub hand-off summary and the dashboard dialog (which mirrors these words in
``GrantDialog``). Pure; the caller passes the function that makes a title safe for one line."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

__all__ = ["event_line", "event_lines"]


def event_line(title: str, interval_s: int, *, credential: bool, show: Callable[[Any], str] = str) -> str:
    """One event in words: what runs, how often, that the add-on decides, that it can reach the internet."""
    minutes = max(interval_s // 60, 1)
    key = "; it uses your stored key" if credential else ""
    return (
        f"{show(title)}: runs in the background every {minutes} min while a follow-up waits for it; the add-on decides "
        f"when the run is finished; it can reach the internet (orch does not limit where){key}"
    )


def event_lines(
    events: Iterable[Mapping[str, Any]], capabilities: Iterable[str], show: Callable[[Any], str] = str
) -> list[str]:
    credential = "credential" in set(capabilities or ())
    return [event_line(e["title"], e["interval_s"], credential=credential, show=show) for e in events]
