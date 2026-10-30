"""The ring path of the webhook inbox: pure logic, no sockets (the listener feeds it).

Order, cheapest first, and the same answer for everything that is not a good ring:

1. a global bucket and the sender's bucket (429, before the token is looked at);
2. the token's shape, then its hash looked up (unknown, revoked, expired: 404, and the sender's *miss* bucket is charged);
3. only for a known hook, the body is read (the listener does that, after the length cap) and the signature checked
   (bad: 404, miss charged);
4. only after the signature passed, the per-hook bucket (over: a silent 204, so 204 never tells an outsider anything
   a valid sender does not already know) and the store: first ring only.

Cookies and bearer tokens are never read here: the only credential read from a header is the hook's own signature
(Databricks: the Basic authorization of that hook), which is checked against that hook's secret and nothing else."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..contract import hooks as HK
from .limits import Buckets
from .store import Hook, HookStore

LOG = logging.getLogger(__name__)
OK, NOT_FOUND, TOO_MANY, TOO_LARGE = 204, 404, 429, 413


@dataclass(frozen=True)
class Limits:
    global_per_min: int = 12_000  # 200 a second
    ip_per_min: int = 30
    ip_burst: int = 30
    miss_per_min: int = 10  # misses (unknown token, bad signature) per sender
    miss_burst: int = 10
    hook_per_min: int = 6
    hook_burst: int = 6


class HookInbox:
    def __init__(
        self,
        store: HookStore,
        now_ms: Callable[[], int],
        url_base: str | None,
        limits: Limits | None = None,
    ) -> None:
        self.store, self.now_ms, self.url_base = store, now_ms, url_base
        lim = limits or Limits()
        self._global = Buckets(now_ms, per_min=lim.global_per_min, burst=lim.global_per_min // 60, cap=1)
        self._ip = Buckets(now_ms, per_min=lim.ip_per_min, burst=lim.ip_burst)
        self._miss = Buckets(now_ms, per_min=lim.miss_per_min, burst=lim.miss_burst)
        self._hook = Buckets(now_ms, per_min=lim.hook_per_min, burst=lim.hook_burst)

    @property
    def enabled(self) -> bool:
        return self.url_base is not None

    def admit(self, source: str) -> bool:
        """Step 1. False: answer 429 without looking at the request any further."""
        return self._global.take("*") and self._ip.take(source) and self._miss.peek(source)

    def find(self, token: str, source: str) -> Hook | None:
        """Step 2: the live hook of a token, or None (the caller answers 404 and reads nothing more)."""
        try:
            th = HK.token_hash(token)
        except ValueError:
            self._miss.take(source)
            return None
        hook = self.store.lookup(th, self.now_ms())
        if hook is None:
            self._miss.take(source)
        return hook

    def finish(self, hook: Hook, headers: Mapping[str, str], body: bytes, source: str) -> int:
        """Steps 3 and 4 with the whole body (already at most BODY_CAP): 204 or the uniform 404."""
        if not self._signed(hook, headers, body):
            self._miss.take(source)
            return NOT_FOUND
        # over the per-hook limit: still a 204, nothing is stored
        if self._hook.take(hook.token_hash) and self.store.ring(hook, self.now_ms()):
            LOG.info("webhook ring for hook %s", hook.token_hash[:8])
        return OK

    def _signed(self, hook: Hook, headers: Mapping[str, str], body: bytes) -> bool:
        # every branch ends in the same constant-time compare; a malformed header is just False
        if hook.source == "github":
            given = HK.parse_github(headers.get("x-hub-signature-256"))
            if given is None:
                return False
            mac = HK.github_mac(hook.secret)
        elif hook.source == "generic":
            p = HK.parse_generic(headers.get("x-orch-signature"))
            if p is None or not HK.generic_fresh(p[0], self.now_ms() // 1000):
                return False
            given, mac = p[1], HK.generic_mac(hook.secret, p[0])
        elif hook.source == "databricks":
            return HK.basic_ok(headers.get("authorization"), hook.secret)
        else:
            return False
        mac.update(body)
        return hmac.compare_digest(mac.hexdigest(), given)
