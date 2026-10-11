"""The peer a kernel UID resolves to: a leaf module, so the direct-mode CLI's guard never loads the transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Peer"]


@dataclass(frozen=True)
class Peer:
    uid: int
    kind: Literal["person", "agent", "worker", "control"]
    person: str | None = None
