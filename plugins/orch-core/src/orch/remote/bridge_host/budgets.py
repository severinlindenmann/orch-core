"""Sliding-window limits: the refusal budgets (spec §6.1: 10 per minute host-wide, 5 per minute per open pairing
offer) and the fresh-assertion rate limit (D9: 6 per 10 minutes per device). In memory: a restart starts empty."""
from __future__ import annotations

from dataclasses import dataclass, field

BUDGET, BUDGET_WINDOW_MS = 10, 60_000
OFFER_BUDGET = 5
FRESH_LIMIT, FRESH_WINDOW_MS = 6, 600_000


@dataclass
class SlidingLimit:
    limit: int
    window_ms: int
    entries: list[int] = field(default_factory=list)

    def take(self, now_ms: int) -> bool:
        """Count one use at `now_ms` if fewer than `limit` uses are younger than the window (an entry exactly
        `window_ms` old no longer counts); False, counting nothing, when the limit is reached."""
        self.entries = [t for t in self.entries if now_ms - t < self.window_ms]
        if len(self.entries) >= self.limit:
            return False
        self.entries.append(now_ms)
        return True


def refusal_budget() -> SlidingLimit:
    return SlidingLimit(BUDGET, BUDGET_WINDOW_MS)


def offer_budget() -> SlidingLimit:
    return SlidingLimit(OFFER_BUDGET, BUDGET_WINDOW_MS)


def fresh_limit() -> SlidingLimit:
    return SlidingLimit(FRESH_LIMIT, FRESH_WINDOW_MS)
