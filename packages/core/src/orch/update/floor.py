"""The revocation floor and freshness rules ``orch update`` adds around :func:`orch.update.verify.verify_envelope`.

The verifier itself only refuses a revocation list OLDER than the stored one; on its own it has no floor and no
freshness. This module adds, for every update (install.sh enforces the same numbers for a first install):

* the baked floor below: the oldest revocation list and the lowest ``min_serial`` ever accepted;
* never below what the install record stored (``revocations_issued_at`` and ``min_serial`` only go up);
* a revocation list no older than :data:`REVOCATIONS_MAX_AGE_DAYS` days (a freeze of the revocation feed shows up as
  an error instead of silently keeping a revoked key alive).

``scripts/release/floor.py`` rewrites the two FLOOR lines (same shape as in install.sh) once per release.
"""

from __future__ import annotations

from datetime import datetime

from orch.ops.errors import OrchError

from .envelope import parse_time
from .verify import Verified

__all__ = [
    "REVOCATIONS_FLOOR_ISSUED_AT",
    "REVOCATIONS_FLOOR_MIN_SERIAL",
    "REVOCATIONS_MAX_AGE_DAYS",
    "check_revocations",
]

# FLOOR-BEGIN  the oldest revocation list an update accepts (scripts/release/floor.py rewrites both lines per release)
REVOCATIONS_FLOOR_ISSUED_AT = "2026-10-20T00:00:00Z"
REVOCATIONS_FLOOR_MIN_SERIAL = 1
# FLOOR-END
REVOCATIONS_MAX_AGE_DAYS = 30


def check_revocations(
    v: Verified, now: datetime, *, stored_issued_at: str | None, stored_min_serial: int | None
) -> None:
    """Raise ``update.unverified`` (below a floor) or ``update.stale`` (too old). Changes nothing."""
    issued = parse_time(v.revocations_issued_at)
    floor_at = parse_time(REVOCATIONS_FLOOR_ISSUED_AT)
    if stored_issued_at is not None:
        floor_at = max(floor_at, parse_time(stored_issued_at))
    floor_serial = max(REVOCATIONS_FLOOR_MIN_SERIAL, stored_min_serial or 0)
    if v.min_serial < floor_serial or issued < floor_at:
        raise OrchError(
            "update.unverified", "the release's revocation list is older than the one this orch already knows"
        )
    if (now - issued).days >= REVOCATIONS_MAX_AGE_DAYS:
        raise OrchError("update.stale", "the release's revocation list is more than 30 days old; nothing was changed")
