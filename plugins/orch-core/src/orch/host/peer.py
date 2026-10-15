"""The peer a kernel UID resolves to: a leaf module, so the direct-mode CLI's guard never loads the transport.

A ``relay`` peer (orch-v2-host §11A.4) has no kernel UID: ``uid = -1`` and the verified device's person. It never
comes from a socket (the transport classifies only kernel credentials) and ``columns.resolve`` refuses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Peer"]


@dataclass(frozen=True)
class Peer:
    uid: int
    kind: Literal["person", "agent", "worker", "control", "runner", "relay"]
    person: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "relay":
            if self.uid != -1 or not isinstance(self.person, str) or not self.person:
                raise ValueError("a relay peer has uid -1 and its device's person")
        elif type(self.uid) is not int or self.uid < 0:
            raise ValueError("a socket peer has a kernel UID")
