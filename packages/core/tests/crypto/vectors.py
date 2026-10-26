"""Loader for the suite-2 section of the protocol vectors: the one relay corpus, ``contracts/relay/`` (contract
11A.10), read through ``tests/wsproto/corpus.py``."""

from __future__ import annotations

from ..wsproto.corpus import KEYS, S2, priv, pub
from ..wsproto.corpus import VECTORS as ALL

__all__ = ["ALL", "KEYS", "S2", "priv", "pub"]
