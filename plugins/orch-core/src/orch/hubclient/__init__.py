"""Core's own hub client (V3, C2a): what a workspace host needs to join a hub selectively and to leave it.

Nothing here imports hub code (D79): the contract (orch-hub ``docs/protocol.md``) is implemented from its text, on
core's own canonical JSON and suite 2. The real TLS link is C5; this package defines the :class:`HubTransport` it plugs
into. Selective mode only: the hub never receives ``WK_e`` and the workspace gets no ``hub`` member (spec 8a, D80).
"""

from __future__ import annotations

from .errors import HubRefused
from .link import HubLink, HubTransport
from .service import HubState, JoinResult, LeaveResult, flush_outbox, join_selective, leave

__all__ = [
    "HubLink",
    "HubRefused",
    "HubState",
    "HubTransport",
    "JoinResult",
    "LeaveResult",
    "flush_outbox",
    "join_selective",
    "leave",
]
