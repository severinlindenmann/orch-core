"""The reader's one refusal: the repository cannot be observed (``observe.unavailable``), or its identity differs
from the declared ``remote`` (``repo.remote_mismatch``, F7)."""

from __future__ import annotations

__all__ = ["Busy", "NotFound", "RemoteMismatch", "Unavailable"]


class Unavailable(Exception):
    """The repository cannot be observed. ``reason`` is a short English sentence, safe to show."""

    code = "observe.unavailable"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RemoteMismatch(Unavailable):
    code = "repo.remote_mismatch"


class Busy(Unavailable):
    """Too many reads waiting for a reader child (section 0.11: queue bounds); retryable."""

    code = "quota.host"


class NotFound(Unavailable):
    """A ``head``, ``base`` or ``since`` the ticket's authorized history does not contain (H6c).
    The reason is fixed text:
    a commit that does not exist and one that exists outside the authorized ranges read alike."""

    code = "not_found"
