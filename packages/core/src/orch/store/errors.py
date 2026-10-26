"""Store errors: one exception with a stable ``code`` (ticket-format §10.4a).

The codes are the model's refusal codes (an event the model refuses is raised as its ``Code``), plus the store's own:
``chain.broken``, ``chain.diverged``, ``trust.genesis_mismatch``, ``store.torn_write`` (reported, not raised, unless
a caller asks), ``store.read_only``, ``store.busy`` and ``validation.*`` (the event or its files break a schema or a
text rule before the model sees them).
"""

from __future__ import annotations

from orch.model import Refusal

__all__ = ["StoreError"]


class StoreError(Exception):
    """``code`` is the stable token; ``refusal`` is set when the model refused the event."""

    def __init__(self, code: str, detail: str = "", *, refusal: Refusal | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.refusal = refusal

    @classmethod
    def from_refusal(cls, refusal: Refusal) -> StoreError:
        return cls(refusal.code.value, refusal.detail, refusal=refusal)
