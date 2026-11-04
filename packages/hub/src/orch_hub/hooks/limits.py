"""Token buckets in a bounded LRU: every operation is O(1), no scan ever (unlike the web API's ``_Limiter``)."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable


class Buckets:
    """``take(key)`` spends one token of ``key``'s bucket (capacity ``burst``, refilled at ``per_min`` a minute).

    At most ``cap`` keys are kept: a new key beyond that evicts the least recently used one, so memory is bounded and an
    attacker cycling keys only forgets old counters (each key starts full, so eviction can only help the sender, which
    is why the per-IP buckets also have a global bucket in front of them)."""

    def __init__(self, now_ms: Callable[[], int], *, per_min: float, burst: int, cap: int = 10_000) -> None:
        self._now, self._rate, self._burst, self._cap = now_ms, per_min / 60_000.0, float(burst), cap
        self._d: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            tokens, last = self._d.pop(key, (self._burst, now))
            tokens = min(self._burst, tokens + max(0, now - last) * self._rate)
            ok = tokens >= 1.0
            if ok:
                tokens -= 1.0
            self._d[key] = (tokens, now)  # most recently used last
            if len(self._d) > self._cap:
                self._d.popitem(last=False)
            return ok

    def peek(self, key: str) -> bool:
        """Whether ``take`` would succeed, without spending (and without touching the LRU order)."""
        now = self._now()
        with self._lock:
            tokens, last = self._d.get(key, (self._burst, now))
            return min(self._burst, tokens + max(0, now - last) * self._rate) >= 1.0

    def __len__(self) -> int:
        return len(self._d)
