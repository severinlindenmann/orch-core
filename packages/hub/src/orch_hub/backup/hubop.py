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
from typing import Self

from ..dataformat import Handle, lock_shared, open_data

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


def open_state(state: Path, *, check_format: bool = True) -> Handle:
    """Open the state dir at its format (see :mod:`orch_hub.dataformat`) and hold the shared format lock. For a command
    that works on the state and then ends: close the handle when done. Every handle that holds the lock for this process is
    made here (so :class:`SwapLock` finds it); ``check_format=False`` is the lock without the format check, for a command
    that makes or replaces the data."""
    handle = open_data(state, DATA_KIND, DATA_FORMAT, check_format=check_format)
    _OPEN.add(handle)
    return handle


class SwapLock:
    """The format lock across a swap of the state dir. ``flock`` follows the file, not the path, so a handle that locked the
    old dir would hold nothing on the new one. Use as ``with SwapLock(state, incoming) as lock:``: it takes the shared lock
    on ``incoming/format.lock`` BEFORE ``incoming`` is renamed to ``state`` (the lock moves with the file: no moment without
    it, an ``orch update`` cannot slip in between), also once for every handle this process holds on ``state``. After the
    rename call :meth:`commit`: the handles take their new descriptors. Until then (and after a failed swap) they keep the
    old ones. A lock only: an older backup's data is refused by the next open (``format.older``), not here.

    Raises :class:`DataFormatError` (``update.busy``) when an update holds ``incoming`` or the dir; nothing is renamed yet."""

    def __init__(self, state: Path, incoming: Path) -> None:
        self._guard: int | None = None
        self._pending: list[tuple[Handle, int]] = []
        try:
            self._guard = lock_shared(incoming)  # held for the whole swap, even when no handle exists
            here = os.path.realpath(state)
            for handle in list(_OPEN):
                if handle._fd is not None and os.path.realpath(handle.path) == here:
                    self._pending.append((handle, lock_shared(incoming)))
        except BaseException:
            self.close()
            raise

    def commit(self) -> None:
        """The rename is done: every handle of this process on the state holds the lock of the new dir."""
        pending, self._pending = self._pending, []
        for handle, fd in pending:
            handle.adopt(fd)

    def close(self) -> None:
        """Let go of what was not handed over (the guard, and the descriptors of an uncommitted swap)."""
        guard, self._guard = self._guard, None
        if guard is not None:
            os.close(guard)
        pending, self._pending = self._pending, []
        for _handle, fd in pending:
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def serving(state: Path) -> bool:
    """True while this process serves ``state`` (``orch-hub serve`` holds its handle: :func:`hold_state`)."""
    here = os.path.realpath(state)
    return any(os.path.realpath(handle.path) == here for handle in _DATA)


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
