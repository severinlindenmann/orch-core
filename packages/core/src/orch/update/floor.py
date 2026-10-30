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

from datetime import datetime, timedelta

from orch.ops.errors import OrchError

from .envelope import parse_time
from .verify import Verified

__all__ = [
    "REVOCATIONS_FLOOR_ISSUED_AT",
    "REVOCATIONS_FLOOR_MIN_SERIAL",
    "REVOCATIONS_MAX_AGE_DAYS",
    "REVOCATIONS_WARN_DAYS",
    "check_revocations",
    "expiry_warning",
]

# FLOOR-BEGIN  the oldest revocation list an update accepts (scripts/release/floor.py rewrites both lines per release)
REVOCATIONS_FLOOR_ISSUED_AT = "2026-10-20T00:00:00Z"
REVOCATIONS_FLOOR_MIN_SERIAL = 1
# FLOOR-END
REVOCATIONS_MAX_AGE_DAYS = 30


REVOCATIONS_WARN_DAYS = 10


def expiry_warning(issued_at: str | None, now: datetime) -> str | None:
    """A plain sentence when the INSTALLED revocation list (``issued_at`` from the install record) expires within
    :data:`REVOCATIONS_WARN_DAYS` days; None otherwise (also when it has already expired: then updates are refused)."""
    if issued_at is None:
        return None
    expires = parse_time(issued_at) + timedelta(days=REVOCATIONS_MAX_AGE_DAYS)
    if timedelta(0) < expires - now <= timedelta(days=REVOCATIONS_WARN_DAYS):
        return f"the update signature list expires on {expires.date()}: ask your admin to renew it"
    return None


def check_revocations(
    v: Verified, now: datetime, *, stored_issued_at: str | None, stored_min_serial: int | None
) -> None:
    """Raise ``update.unverified`` (below a floor) or ``update.stale`` (too old). Changes nothing."""
    issued = parse_time(v.revocations_issued_at)
    floor_at = parse_time(REVOCATIONS_FLOOR_ISSUED_AT)
    if stored_issued_at is not None:
        floor_at = max(floor_at, parse_time(stored_issued_at))
    floor_serial = max(REVOCATIONS_FLOOR_MIN_SERIAL, stored_min_serial or 0)
    if issued > now + timedelta(days=1):
        raise OrchError("update.unverified", "the release's revocation list is dated in the future")
    if v.min_serial < floor_serial or issued < floor_at:
        raise OrchError(
            "update.unverified", "the release's revocation list is older than the one this orch already knows"
        )
    if (now - issued).days >= REVOCATIONS_MAX_AGE_DAYS:
        raise OrchError(
            "update.stale",
            f"the update signature list expired on {(issued + timedelta(days=REVOCATIONS_MAX_AGE_DAYS)).date()}: "
            "ask your admin to renew it. Nothing was changed",
        )
