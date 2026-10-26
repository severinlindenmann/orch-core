"""Core's own hub client (V3, C2a): what a workspace host needs to join a hub selectively and to leave it.

Nothing here imports hub code (D79): the contract (``contracts/hub/protocol.md``) is implemented from its text, on
core's own canonical JSON and suite 2. The real TLS link is C5; this package defines the :class:`HubTransport` it plugs
into. Selective mode (spec 8a, D80) never gives the hub ``WK_e`` or a ``hub`` member; only the full path (C2b:
``join_full``, ``switch_full``) seals the current ``WK_e`` to the hub's ``HXK``.
"""

from __future__ import annotations

from .errors import HubRefused
from .link import HubLink, HubTransport
from .logsync import LogSource, Position, SyncReport, check_proj, derive_view_key, pending, proj_digest, sync_logs
from .service import (
    EpochKeys,
    HubState,
    JoinResult,
    LeaveResult,
    flush_outbox,
    flush_publish,
    join_full,
    join_selective,
    leave,
    retire_full_state,
    seal_to_hub,
    settle_rotation,
    switch_full,
)

__all__ = [
    "EpochKeys",
    "HubLink",
    "HubRefused",
    "HubState",
    "HubTransport",
    "JoinResult",
    "LogSource",
    "LeaveResult",
    "Position",
    "SyncReport",
    "flush_outbox",
    "flush_publish",
    "join_full",
    "join_selective",
    "leave",
    "retire_full_state",
    "seal_to_hub",
    "settle_rotation",
    "check_proj",
    "derive_view_key",
    "proj_digest",
    "pending",
    "sync_logs",
    "switch_full",
]
