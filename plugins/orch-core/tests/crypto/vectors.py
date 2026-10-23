"""Loader for the suite-2 section of the protocol vectors: the one vendored relay corpus, ``tests/wsproto/vectors/``
(contract 11A.10; relay commit, MANIFEST revision and file hash in ``tests/wsproto/vectors/SOURCE``), read through
``tests/wsproto/corpus.py`` (the file is stored xz-compressed)."""

from __future__ import annotations

from ..wsproto.corpus import KEYS, S2, priv, pub
from ..wsproto.corpus import VECTORS as ALL

__all__ = ["ALL", "KEYS", "S2", "priv", "pub"]
