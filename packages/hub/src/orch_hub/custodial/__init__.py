"""The custodial signing engine (protocol.md §8, spec §5 "prepare -> recompute -> sign"). Dev only in V3 (phase 4).

The hub signs a user's event with that user's ``UK`` only when (1) it built the event itself from its own ``WK_e`` view
of the workspace, (2) a valid channel proof from that user over the event digest arrived, and (3) the event it
recomputes at signing time is byte-identical to the one the channel was shown. No caller can hand the engine event bytes.
"""

from .engine import (
    BuiltEvent,
    ChannelRegistry,
    CustodialEngine,
    Prepared,
    SessionCheck,
    WorkspaceView,
)

__all__ = [
    "BuiltEvent",
    "ChannelRegistry",
    "CustodialEngine",
    "Prepared",
    "SessionCheck",
    "WorkspaceView",
]
