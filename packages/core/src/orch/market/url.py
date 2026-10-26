"""``canonical_market_url``: the one place that decides what a marketplace (or add-on) URL looks like (spec 4.1).
It rejects; it does not repair."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from orch import addons

from . import MarketError
from .constants import CASE_INSENSITIVE_HOSTS

SCHEMA_PATH = Path(addons.__file__).resolve().parent / "schemas" / "market.v1.json"


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@lru_cache(maxsize=1)
def _pattern() -> re.Pattern[str]:
    return re.compile(schema()["$defs"]["gitUrl"]["pattern"])


def canonical_market_url(url: object) -> str:
    """``url`` itself when it is in canonical form, else ``market.malformed``. The shape is the schema's ``gitUrl``
    (https, lower-case ASCII dotted DNS name ending in letters, no port, credentials, query, fragment, trailing
    slash or ``.git``); on hosts that ignore case the whole URL must be lower case."""
    if not isinstance(url, str) or not url.isascii() or _pattern().search(url) is None:
        raise MarketError("market.malformed")
    host = url.split("/")[2]
    if host in CASE_INSENSITIVE_HOSTS and url != url.lower():
        raise MarketError("market.malformed")
    return url


def host_of(url: str) -> str:
    return canonical_market_url(url).split("/")[2]
