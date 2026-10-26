"""The ownership lock and the direct-mode guard (D-P2-18).

``<state>/hosts/<ws>/owner.lock`` is taken **exclusive** by the host while it adopts a workspace (register: lock,
write the registration, start serving, unlock) and **shared** by a direct-mode CLI around each store transaction.
Under the shared lock the CLI checks the registration again, every time, and answers ``host.required`` once the
workspace is registered: there is no silent fallback to direct mode.

Shared holders leave ``owner.holders/<pid>-<token>`` while they hold the lock, so a ``register`` that gives up after
60 s can name the holders' PIDs in ``lock.busy`` (F14).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .custody import read_json
from .errors import HostError
from .pins import AgentPins
from .registry import registry_path

__all__ = ["LOCK_WAIT_SECONDS", "DirectGuard", "OwnershipLock", "is_registered"]

LOCK_WAIT_SECONDS = 60.0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class OwnershipLock:
    def __init__(self, state_dir: Path, workspace_id: str) -> None:
        self.dir = Path(state_dir) / "hosts" / workspace_id
        self.path = self.dir / "owner.lock"
        self.holders_dir = self.dir / "owner.holders"

    def _open(self) -> int:
        self.dir.parent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.dir.parent, self.dir):  # hosts/ and hosts/<ws>/: 0700 whatever the umask
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                continue
            os.chmod(path, 0o700)
        return os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)

    def holders(self) -> list[int]:
        pids = set()
        with contextlib.suppress(FileNotFoundError):
            for name in os.listdir(self.holders_dir):
                pid = name.partition("-")[0]
                if pid.isdigit() and _alive(int(pid)):
                    pids.add(int(pid))
        return sorted(pids)

    def _poll(self, fd: int, flags: int, timeout: float, sleep: Callable[[float], None]) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, flags | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                sleep(0.02)

    @contextlib.contextmanager
    def exclusive(
        self, timeout: float = LOCK_WAIT_SECONDS, *, sleep: Callable[[float], None] = time.sleep
    ) -> Iterator[None]:
        """The host's adoption lock: ``lock.busy`` (naming the holders' PIDs) after ``timeout`` seconds."""
        fd = self._open()
        try:
            if not self._poll(fd, fcntl.LOCK_EX, timeout, sleep):
                pids = self.holders()
                named = ", ".join(map(str, pids)) if pids else "unknown"
                raise HostError(
                    "lock.busy",
                    f"the ownership lock of {self.dir.name} is held (PIDs: {named})",
                    retry_after_ms=1000,
                )
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def shared(self, timeout: float) -> Iterator[bool]:
        """A direct-mode transaction: yields ``False`` if the lock could not be taken in time."""
        fd = self._open()
        marker: Path | None = None
        try:
            if not self._poll(fd, fcntl.LOCK_SH, timeout, time.sleep):
                yield False
                return
            try:
                self.holders_dir.mkdir(exist_ok=True, mode=0o700)
                marker = self.holders_dir / f"{os.getpid()}-{secrets.token_hex(4)}"
                marker.touch(mode=0o600)
                yield True
            finally:
                if marker is not None:
                    with contextlib.suppress(FileNotFoundError):
                        marker.unlink()
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def is_registered(state_dir: Path, workspace_id: str, *, uid: int | None = None, pins: AgentPins | None = None) -> bool:
    """Whether ``workspace_id`` is host-managed for this UID: in the person's own registry, or in the trusted pin of an
    agent UID. An unreadable registry fails closed (it counts as registered)."""
    uid = os.getuid() if uid is None else uid
    if (pins or AgentPins()).names_workspace(uid, workspace_id):
        return True
    try:
        doc = read_json(registry_path(state_dir), uid)
    except HostError:
        return True
    if doc is None:
        return False
    try:
        return any(item.get("workspace_id") == workspace_id for item in doc["workspaces"])
    except (KeyError, TypeError, AttributeError):
        return True


class DirectGuard:
    """``Store(guard=DirectGuard(...))``: each outermost store transaction holds the ownership lock shared and checks
    the registration. Re-entrant per thread, so nested store calls take the lock once."""

    def __init__(
        self,
        state_dir: Path,
        workspace_id: str,
        *,
        timeout: float = 10.0,
        uid: int | None = None,
        pins: AgentPins | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.workspace_id = workspace_id
        self.lock = OwnershipLock(self.state_dir, workspace_id)
        self.timeout, self.uid, self.pins = timeout, uid, pins
        self._local = threading.local()

    def check(self) -> None:
        if is_registered(self.state_dir, self.workspace_id, uid=self.uid, pins=self.pins):
            from orch.ops.errors import OrchError

            raise OrchError(
                "host.required",
                "this workspace is served by the orch host; direct mode is off",
                hint="start the host service, or orch host unregister to return to direct mode",
            )

    @contextlib.contextmanager
    def __call__(self) -> Iterator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return
        self.check()  # before waiting: a registered workspace answers at once
        with self.lock.shared(self.timeout) as held:
            if not held:
                from orch.store import StoreError

                raise StoreError("store.busy", "the host is adopting this workspace")
            self.check()  # again under the lock: the registration may have landed while we waited
            self._local.depth = 1
            try:
                yield
            finally:
                self._local.depth = 0
