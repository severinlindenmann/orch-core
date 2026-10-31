"""One heavy hub operation at a time (M4, Opus condition 20): a restore, and later an update or a remove, each hold this
lock for their whole run, so none of them starts while another runs. ``flock`` on ``.<state name>.hubop.lock`` NEXT TO the state
dir (0600; a restore swaps the state dir, so the lock cannot live inside it), taken without waiting: a second operation is refused (``HubOpBusy``), not queued. The kernel
drops the lock when the holder dies, so a crash never leaves the hub stuck."""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
import weakref
from collections.abc import Iterator
from pathlib import Path

from ..dataformat import Handle, open_data

KINDS = ("restore", "update", "remove")
#: Set by a console restore for its whole run (and left set once it succeeded: the hub restarts). While it is set the web
#: pipeline refuses every mutating route and the link server every op with ``restore_busy``, so no write lands in a state
#: dir that is about to be swapped away. Reads keep working. Cleared only when the restore failed.
FENCE = threading.Event()


class HubOpBusy(Exception):
    """Another heavy operation (``kind``) holds the lock; ``code`` is stable."""

    def __init__(self, kind: str = "", code: str = "restore_busy") -> None:
        super().__init__(kind)
        self.code = code


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


_HELD: dict[str, int] = {}


def _serve_lock_path(state: Path) -> Path:
    state = Path(state)
    return state.parent / f".{state.name}.serve.lock"


def hold_serve_lock(state: Path) -> int:
    """``orch-hub serve`` holds this exclusive ``flock`` for its whole life (the returned descriptor stays open, close-on-exec,
    and the kernel drops it when the process dies). A second server on the same state, or an offline restore by the server
    command, is refused. Raises :class:`HubOpBusy` (code ``hub_running``) when it is held."""
    key = str(_serve_lock_path(state))
    stale = _HELD.pop(key, None)
    if (
        stale is not None
    ):  # this process served before (it returned or failed): its old descriptor is not a second server
        with contextlib.suppress(OSError):
            os.close(stale)
    fd = os.open(_serve_lock_path(state), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise HubOpBusy("serve", "hub_running") from None
    _HELD[key] = fd
    return fd


DATA_KIND = "hub"
DATA_FORMAT = 1  # the format of the hub state dir this code reads and writes (format.json "format")
_DATA: list[Handle] = []  # the handle of the serving process: the shared format lock lives as long as it is referenced


_OPEN: weakref.WeakSet[Handle] = weakref.WeakSet()  # every handle open_state made and nobody closed or dropped yet


def open_state(state: Path) -> Handle:
    """Open the state dir at its format (see :mod:`orch_hub.dataformat`) and hold the shared format lock. For a command
    that works on the state and then ends: close the handle when done."""
    handle = open_data(state, DATA_KIND, DATA_FORMAT)
    _OPEN.add(handle)
    return handle


def relock_state(state: Path) -> None:
    """The state dir was swapped (an in-place restore, or :func:`orch_hub.backup.restore.recover` put a crashed swap right).
    The format lock follows the file, not the path, so every handle of this process on this state takes the shared lock
    again on the new dir's ``format.lock``: call it right after the rename, before anything else touches the new dir (an
    ``orch update`` could take it exclusive). A lock only: the data may be at an older format after a restore, which the
    next open refuses with ``format.older``. Raises :class:`DataFormatError` (``update.busy``) if an update got there first."""
    here = os.path.realpath(state)
    for handle in list(_OPEN):
        if os.path.realpath(handle.path) == here:
            handle.relock()


def hold_state(state: Path) -> Handle:
    """``orch-hub serve``: :func:`open_state`, kept for the whole life of the process (a serve that ran before in this
    process, e.g. in a test, gives its old handle up first). Raises :class:`DataFormatError`."""
    release_state()
    handle = open_state(state)
    _DATA.append(handle)
    return handle


def release_state() -> None:
    """Drop the shared format lock of this process's serve (it has stopped serving)."""
    while _DATA:
        _DATA.pop().close()


def ensure_not_running(state: Path) -> None:
    """Refuse (``hub_running``) while a hub serves this state dir."""
    key = str(_serve_lock_path(state))
    if key in _HELD:  # held by this very process (a test): not "another" hub
        return
    os.close(hold_serve_lock(state))
    _HELD.pop(key, None)
