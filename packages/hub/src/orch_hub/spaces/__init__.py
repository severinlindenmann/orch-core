"""Shared spaces of the hub (protocol.md §10.4, V3-D8): store, key epochs, wraps, the link ops' logic."""

from .service import SpaceError, SpaceService
from .store import SpaceStore

__all__ = ["SpaceError", "SpaceService", "SpaceStore"]
