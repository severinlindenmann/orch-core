"""The hub marketplace policy ``orch.hub-policy/1`` (D14, orch-core#927; protocol.md §7.1).

One hub-wide object that rides inside every ``hub.snapshot`` as the required field ``market_policy``. The hub
refuses to store, and orch-core refuses to accept, anything this validator calls malformed: exact keys at every level,
sorted unique lists, canonical marketplace URLs, ``class`` recomputed from the URL, never trusted. Malformed anywhere
means the whole policy is malformed (no partial reading)."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from .canon import b64u, cj
from .labels import L

OFFICIAL_URL = "https://github.com/severinlindenmann/orch-marketplace"  # orch-core orch.market.constants
CAPABILITIES = ("git_push", "network", "pty", "serve_http", "spawn_agent")  # orch-core addons.manifest
CASE_INSENSITIVE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
MAX_MARKETS, MAX_KEYS, MAX_IDS = 16, 2, 256  # these bound cj(policy) below 48 KiB

_GIT_URL = re.compile(
    r"(?!.*\.git\Z)https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,5}[a-z]{2,24}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._~-]{0,99}){1,6}\Z"
)
_ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
_SEMVER = r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})"
_BLOCK_ITEM = re.compile(r"[a-z][a-z0-9-]{0,39}(?:@" + _SEMVER + r")?\Z")
_B64U_KEY = re.compile(r"[A-Za-z0-9_-]{86}[AEIMQUYcgkosw048]\Z")


class PolicyMalformed(ValueError):
    pass


def default_policy() -> dict[str, Any]:
    """A new hub: the official marketplace only, every add-on, no capability ceiling."""
    return {
        "markets": [{"url": OFFICIAL_URL, "class": "official", "keys": []}],
        "addons": {"allow": None, "block": []},
        "ceiling": None,
    }


def _canonical_url(u: Any) -> bool:
    if type(u) is not str or not _GIT_URL.match(u):
        return False
    return not (u.split("/")[2] in CASE_INSENSITIVE_HOSTS and u != u.lower())


def _valid_key(k: Any) -> bool:
    if type(k) is not str or not _B64U_KEY.match(k):
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.urlsafe_b64decode(k + "="))
    except ValueError:
        return False
    return True


def _sorted_unique(xs: Any, lo: int, hi: int, ok: Any) -> None:
    if type(xs) is not list or not lo <= len(xs) <= hi or not all(ok(x) for x in xs) or xs != sorted(set(xs)):
        raise PolicyMalformed


def _is(rx: re.Pattern[str]) -> Any:
    return lambda x: type(x) is str and bool(rx.match(x))


def check_policy(p: Any) -> dict[str, Any]:
    """Return ``p`` if it is a valid policy, else raise :class:`PolicyMalformed`."""
    if type(p) is not dict or set(p) != {"markets", "addons", "ceiling"}:
        raise PolicyMalformed
    ms = p["markets"]
    if type(ms) is not list or len(ms) > MAX_MARKETS:
        raise PolicyMalformed
    for m in ms:
        if type(m) is not dict or set(m) != {"url", "class", "keys"} or not _canonical_url(m["url"]):
            raise PolicyMalformed
        if m["class"] == "official":
            if m["url"] != OFFICIAL_URL or m["keys"] != []:
                raise PolicyMalformed
        elif m["class"] == "company":
            if m["url"] == OFFICIAL_URL:
                raise PolicyMalformed
            _sorted_unique(m["keys"], 1, MAX_KEYS, _valid_key)
        else:
            raise PolicyMalformed
    urls = [m["url"] for m in ms]
    if urls != sorted(set(urls)):
        raise PolicyMalformed
    a = p["addons"]
    if type(a) is not dict or set(a) != {"allow", "block"}:
        raise PolicyMalformed
    if a["allow"] is not None:
        _sorted_unique(a["allow"], 0, MAX_IDS, _is(_ID))
    _sorted_unique(a["block"], 0, MAX_IDS, _is(_BLOCK_ITEM))
    if p["ceiling"] is not None:
        _sorted_unique(p["ceiling"], 0, len(CAPABILITIES), lambda x: type(x) is str and x in CAPABILITIES)
    return p


def valid_policy(p: Any) -> bool:
    try:
        check_policy(p)
    except PolicyMalformed:
        return False
    return True


def policy_hash(p: dict[str, Any]) -> str:
    """``b64u(H("orch/v2/hub-market-policy|" || cj(policy)))`` of a valid policy."""
    return b64u(hashlib.sha256(L["h_hub_market_policy"] + cj(p)).digest())
