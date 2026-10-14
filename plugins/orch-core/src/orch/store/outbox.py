"""The relay outbox (§11 P3-D21, §11A.3): ``<state>/hosts/<ws>/outbox/``, written only by the store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["OutboxEntry", "Segment"]


@dataclass(frozen=True)
class Segment:
    log: str
    after_seq: int
    from_acc: str
    heads: tuple[str, ...]


@dataclass(frozen=True)
class OutboxEntry:
    n: int
    hash: str
    kind: str
    checkpoint: dict[str, Any]
    restore: dict[str, Any] | None
    catchup: bool
