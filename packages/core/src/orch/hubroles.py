"""The one rule for "what role does a hub user hold in a verified ``hub.snapshot`` object at time t" (GA-07).

A neutral module with no imports of its own, so the hub client (``hubclient.wire``, ``hubclient.snapshot``,
``hubclient.custodial``) and the model (``model.identity``) use the SAME function. The model passes the deterministic
time of the event it is judging (its ``at``), never the wall clock, so replays are stable; the hub client its clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: a snapshot issued more than this far ahead of the verifier's clock is not trusted for any user with an end date (R6)
SNAPSHOT_SKEW_MS = 5 * 60 * 1000


def role_at(snapshot: Mapping[str, Any], user_id: str, now_ms: int) -> str | None:
    """THE role of ``user_id`` in a verified snapshot object at ``now_ms`` (GA-07; hub protocol 7). A user listed in
    ``ends_ms`` has no role from that time on (fail closed: the true role may only have been lowered, a fresher
    snapshot says so), and none when the snapshot claims to be issued more than 5 minutes ahead of this clock (a host
    clock set back must not extend access). Every role decision on the host (reads and the signing path) goes
    through this one function."""
    role = snapshot["roles"].get(user_id)
    end = snapshot["ends_ms"].get(user_id)
    if role is None or end is None:
        return role
    if now_ms >= end or snapshot["issued_ms"] > now_ms + SNAPSHOT_SKEW_MS:
        return None
    return role
