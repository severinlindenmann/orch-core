"""Loader for the suite-2 section of the protocol vectors: the one vendored relay corpus, ``tests/relay/vectors/``
(contract 11A.10; relay commit, MANIFEST revision and file hash in ``tests/relay/vectors/SOURCE``), read through
``tests/relay/corpus.py`` (the file is stored xz-compressed)."""

from __future__ import annotations

from ..relay.corpus import KEYS, S2, priv, pub
from ..relay.corpus import VECTORS as ALL

__all__ = ["ALL", "KEYS", "S2", "priv", "pub"]
