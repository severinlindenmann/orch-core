"""The reader's one refusal: the repository cannot be observed (``observe.unavailable``), or its identity differs
from the declared ``remote`` (``repo.remote_mismatch``, F7)."""

from __future__ import annotations

__all__ = ["RemoteMismatch", "Unavailable"]


class Unavailable(Exception):
    """The repository cannot be observed. ``reason`` is a short English sentence, safe to show."""

    code = "observe.unavailable"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RemoteMismatch(Unavailable):
    code = "repo.remote_mismatch"
