"""The one display form of a hub content hash (hub protocol 8.4, orch-hub#55): the first 12 lower-case hex digits after
``sha256:`` in three groups of 4, ``abcd ef01 2345``.

Display only: nothing is signed or compared on this value. The input per kind is the hash the person decides on: a gate
or verdict decision uses the projection gate ``hash``, a question its question ``hash``; a status move has no hash and
shows "from -> to" only. Anything that is not ``sha256:`` plus 64 lower-case hex digits is ``malformed`` and the caller
shows the full field instead of a guess. Replayed against the hub vectors ``short_hash_*`` / ``short_digest_*``
(``tests/hub/test_shorthash.py``). D79: standalone, no hub code is imported.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import HubRefused

_HASH = re.compile(r"sha256:([0-9a-f]{12})[0-9a-f]{52}")
_DIGEST = re.compile(r"([0-9a-f]{12})[0-9a-f]{52}")


def _groups(x: str) -> str:
    return " ".join(x[i : i + 4] for i in range(0, 12, 4))


def short_hash(h: Any) -> str:
    """``sha256:3d3f5e93357c...`` as ``3d3f 5e93 357c``; ``HubRefused("malformed")`` for anything else."""
    m = _HASH.fullmatch(h) if type(h) is str else None
    if m is None:
        raise HubRefused("malformed")
    return _groups(m.group(1))


def short_digest(d: Any) -> str:
    """The same grouping for a 64-digit lower-case hex digest (a "signature reference")."""
    m = _DIGEST.fullmatch(d) if type(d) is str else None
    if m is None:
        raise HubRefused("malformed")
    return _groups(m.group(1))
