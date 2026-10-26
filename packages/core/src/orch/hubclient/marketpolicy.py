"""The hub marketplace policy ``orch.hub-policy/1`` as it rides in ``hub.snapshot.market_policy`` (orch-hub
``contracts/hub/protocol.md`` 7.1, D14, orch-core#927).

:func:`parse_policy` is the one validator: exact keys at every level, sorted unique lists, canonical marketplace URLs,
``class`` recomputed from the URL (never trusted), valid P-256 pins; malformed anywhere is the whole policy malformed
(:class:`HubRefused` ``malformed``). It returns a frozen :class:`MarketPolicy`, the read-only value the market package
reads. This module only parses; evaluating a policy is the market package's job (G7, orch-core#928). A policy comes only
from a verified, pinned snapshot (:func:`orch.hubclient.snapshot.hub_market_policy`), never from a hub reply as is."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from orch.addons.manifest import CAPABILITIES
from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as _suite
from orch.crypto.encoding import EncodingError, b64u, unb64u
from orch.market import MarketError
from orch.market.constants import OFFICIAL_URL
from orch.market.url import canonical_market_url

from .errors import HubRefused

__all__ = ["MarketEntry", "MarketPolicy", "parse_policy", "policy_hash"]

MAX_MARKETS, MAX_KEYS, MAX_IDS = 16, 2, 256
_HASH_LABEL = b"orch/v2/hub-market-policy|"
_ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
_SEMVER = r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})"
_BLOCK_ITEM = re.compile(r"[a-z][a-z0-9-]{0,39}(?:@" + _SEMVER + r")?\Z")


@dataclass(frozen=True)
class MarketEntry:
    url: str
    cls: str  # "official" | "company"
    keys: tuple[str, ...]  # company: 1 or 2 pinned P-256 keys (b64u); official: ()


@dataclass(frozen=True)
class MarketPolicy:
    markets: tuple[MarketEntry, ...]
    allow: tuple[str, ...] | None  # None = any add-on id
    block: tuple[str, ...]  # ``id`` (every version) or ``id@x.y.z``
    ceiling: tuple[str, ...] | None  # None = no capability ceiling

    def market(self, url: str) -> MarketEntry | None:
        return next((m for m in self.markets if m.url == url), None)


def _bad() -> HubRefused:
    return HubRefused("malformed")


def _key_ok(k: Any) -> bool:
    try:
        _suite.validate_public_key(unb64u(k, 65))
    except (JcsError, EncodingError, _suite.CryptoError):
        return False
    return True


def _list(xs: Any, lo: int, hi: int, ok: Any) -> tuple[str, ...]:
    if type(xs) is not list or not lo <= len(xs) <= hi or not all(ok(x) for x in xs) or xs != sorted(set(xs)):
        raise _bad()
    return tuple(xs)


def _rx(rx: re.Pattern[str]) -> Any:
    return lambda x: type(x) is str and rx.match(x) is not None


def parse_policy(p: Any) -> MarketPolicy:
    if type(p) is not dict or set(p) != {"markets", "addons", "ceiling"}:
        raise _bad()
    ms = p["markets"]
    if type(ms) is not list or len(ms) > MAX_MARKETS:
        raise _bad()
    entries: list[MarketEntry] = []
    for m in ms:
        if type(m) is not dict or set(m) != {"url", "class", "keys"}:
            raise _bad()
        try:
            url = canonical_market_url(m["url"])
        except MarketError:
            raise _bad() from None
        if m["class"] == "official":
            if url != OFFICIAL_URL or m["keys"] != []:
                raise _bad()
            entries.append(MarketEntry(url, "official", ()))
        elif m["class"] == "company":
            if url == OFFICIAL_URL:
                raise _bad()
            entries.append(MarketEntry(url, "company", _list(m["keys"], 1, MAX_KEYS, _key_ok)))
        else:
            raise _bad()
    urls = [e.url for e in entries]
    if urls != sorted(set(urls)):
        raise _bad()
    a = p["addons"]
    if type(a) is not dict or set(a) != {"allow", "block"}:
        raise _bad()
    allow = None if a["allow"] is None else _list(a["allow"], 0, MAX_IDS, _rx(_ID))
    block = _list(a["block"], 0, MAX_IDS, _rx(_BLOCK_ITEM))
    ceiling = None
    if p["ceiling"] is not None:
        ceiling = _list(p["ceiling"], 0, len(CAPABILITIES), lambda x: type(x) is str and x in CAPABILITIES)
    return MarketPolicy(tuple(entries), allow, block, ceiling)


def policy_hash(p: Any) -> str:
    """``b64u(H("orch/v2/hub-market-policy|" || cj(policy)))`` of a policy that :func:`parse_policy` accepts."""
    parse_policy(p)
    return b64u(hashlib.sha256(_HASH_LABEL + cj(p)).digest())
