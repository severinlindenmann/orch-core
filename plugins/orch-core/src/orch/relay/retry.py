"""Relay refusals and the retry rules of contract 11A.7 ("Retries and backoff").

* **transient** (transport errors, deadlines, 5xx, 503 ``overloaded``, 429 ``rate_limited``): full-jitter backoff
  1 s .. 5 min; ``Retry-After`` is a floor (integer seconds, capped at 1 h).
* **quota** (429 ``quota_exceeded`` outside membership): 5 retries with backoff, then alarm ``relay_quota`` and 1 h
  between attempts.
* **permanent**, never retried unchanged: 429 ``quota_exceeded`` on membership or naming ``nodes``, ``workspaces``
  or ``evidence``; 400 ``too_large``; 413; every other 4xx; any 3xx.

A retry resends the identical bytes: callers build the body once, outside :meth:`Retrier.run`.
Specific 4xx codes (``holder_conflict``, ``fenced``, ``unauthenticated``) are handled by the session before they reach
the retrier. Waiting uses the injected ``sleep`` (or the stop event), never the wall clock.
"""

from __future__ import annotations

import logging
import random
import re
import threading
from collections.abc import Callable
from typing import TypeVar

from orch.net.http import RedirectRefused, Response, TransportError
from orch.wsproto import wire

__all__ = [
    "PERMANENT",
    "QUOTA",
    "TRANSIENT",
    "RelayRefused",
    "Retrier",
    "Stopped",
    "backoff",
    "classify",
    "delay",
    "parse_retry_after",
]

log = logging.getLogger("orch.relay.retry")

TRANSIENT, QUOTA, PERMANENT = "transient", "quota", "permanent"
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 300.0
RETRY_AFTER_CAP_S = 3600
QUOTA_RETRIES = 5
QUOTA_WAIT_S = 3600.0
PERMANENT_QUOTAS = frozenset({"nodes", "workspaces", "evidence"})
ERROR_BODY_CAP = 16 * 1024

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DIGITS = re.compile(r"[0-9]{1,10}")

T = TypeVar("T")


class RelayRefused(Exception):
    """A relay answer outside 2xx: ``status``, the §14 ``code`` (or ``None`` when the body is not a well-formed
    ``{"error": code, …}``), ``Retry-After`` seconds and the planned ``quota`` detail. Nothing else of the body is
    kept, so a message never carries relay-supplied text beyond a validated code."""

    def __init__(
        self, status: int, code: str | None, *, retry_after: int | None = None, quota: str | None = None, route=""
    ):
        super().__init__(f"relay refused {route}: {status} {code}")
        self.status, self.code, self.retry_after, self.quota, self.route = status, code, retry_after, quota, route

    @classmethod
    def from_response(cls, route: str, r: Response) -> RelayRefused:
        code = quota = None
        try:
            obj = wire.loads_body(r.body, cap=ERROR_BODY_CAP)
            if type(obj) is dict:
                c, q = obj.get("error"), obj.get("quota")
                code = c if type(c) is str and _CODE.fullmatch(c) else None
                quota = q if type(q) is str and _CODE.fullmatch(q) else None
        except wire.WireError:
            pass
        return cls(
            r.status, code, retry_after=parse_retry_after(r.headers.get("retry-after")), quota=quota, route=route
        )


class Stopped(Exception):
    """The worker is stopping; the wait was abandoned."""


def parse_retry_after(value: str | None) -> int | None:
    """Integer delta-seconds (§19), capped at 1 h; anything else is ignored."""
    if value is None or not _DIGITS.fullmatch(value):
        return None
    return min(int(value), RETRY_AFTER_CAP_S)


def classify(err: BaseException, *, membership: bool) -> str:
    if isinstance(err, RedirectRefused):
        return PERMANENT
    if isinstance(err, TransportError):
        return TRANSIENT if err.transient else PERMANENT
    if isinstance(err, RelayRefused):
        if err.status >= 500 or (err.status == 429 and err.code == "rate_limited"):
            return TRANSIENT
        if err.status == 429 and err.code == "quota_exceeded":
            if membership or err.quota in PERMANENT_QUOTAS:
                return PERMANENT
            return QUOTA
        return PERMANENT
    return PERMANENT


def backoff(attempt: int, rng: random.Random) -> float:
    """Full jitter over ``[0, min(5 min, 2^attempt s)]``, never below 1 s."""
    return max(BACKOFF_MIN_S, rng.uniform(0.0, min(BACKOFF_MAX_S, 2.0 ** min(attempt, 30))))


def delay(attempt: int, retry_after: int | None, rng: random.Random) -> float:
    return max(backoff(attempt, rng), float(min(retry_after, RETRY_AFTER_CAP_S)) if retry_after is not None else 0.0)


class Retrier:
    """Runs one relay call under the retry rules; ``alarm(code, detail)`` raises a host alarm."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None],
        alarm: Callable[[str, str], None],
        rng: random.Random | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._sleep, self._alarm, self._rng, self._stop = sleep, alarm, rng or random.SystemRandom(), stop

    def _wait(self, seconds: float) -> None:
        """Wait ``seconds``; with a stop event, the wait itself ends when stop is set (11A.7: join within 5 s)."""
        if self._stop is None:
            self._sleep(seconds)
            return
        if self._stop.wait(seconds):
            raise Stopped()

    def run(self, fn: Callable[[], T], *, route: str, membership: bool = False, max_attempts: int | None = None) -> T:
        attempt = quota_failures = 0
        while True:
            attempt += 1
            try:
                return fn()
            except (RelayRefused, TransportError) as e:
                cls = classify(e, membership=membership)
                if isinstance(e, RedirectRefused):
                    self._alarm("relay_redirect", route)
                if cls == PERMANENT or (max_attempts is not None and attempt >= max_attempts):
                    raise
                retry_after = e.retry_after if isinstance(e, RelayRefused) else None
                if cls == QUOTA:
                    quota_failures += 1
                    if quota_failures > QUOTA_RETRIES:
                        self._alarm("relay_quota", route)
                        log.warning("relay %s: quota_exceeded after %d retries; waiting 1 h", route, QUOTA_RETRIES)
                        self._wait(QUOTA_WAIT_S)
                        continue
                wait = delay(attempt - 1, retry_after, self._rng)
                log.info("relay %s: %s, retry in %.1f s", route, type(e).__name__, wait)
                self._wait(wait)
