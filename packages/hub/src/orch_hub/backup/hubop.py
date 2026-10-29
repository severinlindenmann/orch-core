"""One heavy hub operation at a time (M4, Opus condition 20): a restore, and later an update or a remove, each hold this
lock for their whole run, so none of them starts while another runs. ``flock`` on ``.<state name>.hubop.lock`` NEXT TO the state
dir (0600; a restore swaps the state dir, so the lock cannot live inside it), taken without waiting: a second operation is refused (``HubOpBusy``), not queued. The kernel
drops the lock when the holder dies, so a crash never leaves the hub stuck."""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

KINDS = ("restore", "update", "remove")


class HubOpBusy(Exception):
    """Another heavy operation (``kind``) holds the lock; ``code`` is stable."""

    code = "restore_busy"


@contextlib.contextmanager
def hub_op(state: Path, kind: str) -> Iterator[None]:
    """Hold the hub-operation lock as ``kind`` or raise :class:`HubOpBusy`. Not re-entrant on purpose: an operation never
    starts another (a restore calls no update, an update no restore)."""
    if kind not in KINDS:
        raise ValueError(kind)
    state = Path(state)
    fd = os.open(
        state.parent / f".{state.name}.hubop.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HubOpBusy(kind) from None
        yield
    finally:
        os.close(fd)  # closing releases the flock
