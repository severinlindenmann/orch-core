"""The host side of the reader: a pool of at most four children, the pin store, and the backend of
``Capabilities.git`` in host mode (D-P2-9, D-P2-19, F7, F10).

* :class:`ReaderPool` starts every child through :func:`orch.host.launcher.spawn_gitread` (the one spawn the host's
  audit hook allows). At most :data:`~orch.gitread.limits.MAX_CHILDREN` run at once host-wide, and one per
  principal: the rest queue (and count as in flight for the caller). A child that runs longer than 30 s, or answers
  more than 16 MiB, is killed with its process group and the request is ``observe.unavailable``.
* :class:`PinStore` keeps the pins of each declared working copy, by its real path, in memory and (when given a
  file) in the host's state directory, written atomically with mode 0600.
* :class:`HostGitReader` is what ``Capabilities.host(..., git=...)`` gets. :meth:`HostGitReader.sync` pins the repos
  that appear in ``settings.repos`` (registration, and a ``settings.changed`` that adds or moves one; an existing pin
  is **never** replaced for the same path) and drops the ones that disappeared.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from orch import canon

from . import limits
from .errors import RemoteMismatch, Unavailable
from .fs import RepoPins

__all__ = ["HostGitReader", "PinStore", "ReaderPool"]


class ReaderPool:
    def __init__(
        self,
        *,
        max_children: int = limits.MAX_CHILDREN,
        spawn: Callable[[], Any] | None = None,
        wall: float = limits.WALL_SECONDS,
    ) -> None:
        if spawn is None:
            from orch.host.launcher import spawn_gitread

            spawn = spawn_gitread
        self._spawn = spawn
        self._wall = wall
        self._slots = threading.BoundedSemaphore(max_children)
        self._mu = threading.Lock()
        self._principals: dict[str, list[Any]] = {}  # principal -> [lock, users]
        self.active = 0
        self.peak = 0
        self.started = 0

    @contextlib.contextmanager
    def _principal(self, principal: str):
        with self._mu:
            entry = self._principals.setdefault(principal, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._mu:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._principals[principal]

    def call(self, principal: str, hello: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
        """One request in a fresh child. Raises :class:`Unavailable` (or :class:`RemoteMismatch`)."""
        with self._principal(principal), self._slots:
            with self._mu:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.started += 1
            try:
                resp = self._run(hello, req)
            finally:
                with self._mu:
                    self.active -= 1
        if "ok" in resp and isinstance(resp["ok"], dict):
            return resp["ok"]
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        message = str(err.get("message") or "the reader failed")[:300]
        if err.get("code") == RemoteMismatch.code:
            raise RemoteMismatch(message)
        raise Unavailable(message)

    def _run(self, hello: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
        data = (json.dumps(hello) + "\n" + json.dumps(req) + "\n").encode("utf-8")
        if len(data) > 2 * limits.REQUEST_MAX:
            raise Unavailable("the request is too large")
        deadline = time.monotonic() + self._wall
        try:
            proc = self._spawn()
        except OSError as e:
            raise Unavailable(f"the reader could not start ({type(e).__name__})") from None
        killed = False
        try:
            try:
                proc.stdin.write(data)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # the child died early: its stdout tells, or it says nothing
            buf = bytearray()
            fd = proc.stdout.fileno()
            with selectors.DefaultSelector() as sel:
                sel.register(fd, selectors.EVENT_READ)
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        killed = self._kill(proc)
                        raise Unavailable(f"the reader took longer than {self._wall:g} s")
                    if not sel.select(left):
                        continue
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > limits.RESPONSE_MAX:
                        killed = self._kill(proc)
                        raise Unavailable("the reader's answer is too large")
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:
                killed = self._kill(proc)
        finally:
            if not killed and proc.poll() is None:
                self._kill(proc)
            with contextlib.suppress(OSError):
                proc.stdout.close()
        line = bytes(buf).split(b"\n", 1)[0]
        try:
            resp = json.loads(line)
        except ValueError:
            raise Unavailable("the reader stopped without an answer (a limit was hit)") from None
        if not isinstance(resp, dict):
            raise Unavailable("the reader answered nonsense")
        return resp

    @staticmethod
    def _kill(proc: Any) -> bool:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)  # its own process group (start_new_session)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        return True


class PinStore:
    """Pins by the declared working copy's absolute path, normalised lexically (:func:`_key`). ``path``: a JSON
    file in the host's state directory, or ``None`` (memory only)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._pins: dict[str, RepoPins] = {}
        self._mu = threading.Lock()
        if path is not None and path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("repos"), dict):
                raise ValueError(f"{path}: not a pin file")
            self._pins = {k: RepoPins.from_json(v) for k, v in doc["repos"].items()}

    def get(self, real: str) -> RepoPins | None:
        with self._mu:
            return self._pins.get(real)

    def paths(self) -> list[str]:
        with self._mu:
            return sorted(self._pins)

    def update(self, put: dict[str, RepoPins], drop: Iterable[str]) -> None:
        with self._mu:
            for k in drop:
                self._pins.pop(k, None)
            self._pins.update(put)
            if self.path is not None:
                doc = {"v": 1, "repos": {k: v.to_json() for k, v in sorted(self._pins.items())}}
                tmp = self.path.with_name(self.path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(doc, sort_keys=True))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)


class HostGitReader:
    """The host-mode backend of ``Capabilities.git``: every read is a fresh child under the ``gitdir`` profile."""

    def __init__(self, pool: ReaderPool, pins: PinStore | None = None, *, principal: str = "host") -> None:
        self.pool = pool
        self.pins = pins if pins is not None else PinStore()
        self.principal = principal

    # -- pins
    def sync(self, declared: Iterable[str | os.PathLike[str]]) -> dict[str, str | None]:
        """Pin every declared working copy not pinned yet; drop pins of paths no longer declared. Returns
        ``{real path: None | why it could not be pinned}``. An existing pin is kept as it is (only a
        ``settings.changed`` that moves a repo, which gives it a new path, leads to a new pin)."""
        reals = [_key(p) for p in declared]
        out: dict[str, str | None] = {}
        put: dict[str, RepoPins] = {}
        for real in reals:
            if self.pins.get(real) is not None:
                out[real] = None
                continue
            try:
                hello = {"profile": "pin", "roots": reals}
                got = self.pool.call(self.principal, hello, {"op": "pin", "args": {"path": real}})
                put[real] = RepoPins.from_json(got.get("pins"))
                out[real] = None
            except Unavailable as e:
                out[real] = e.reason
        drop = [p for p in self.pins.paths() if p not in reals]
        if put or drop:
            self.pins.update(put, drop)
        return out

    def _pins(self, path: str | os.PathLike[str]) -> RepoPins:
        pins = self.pins.get(_key(path))
        if pins is None:
            raise Unavailable("the repository was not pinned at registration")
        return pins

    def _call(self, path: str | os.PathLike[str], op: str, args: dict[str, Any], principal: str | None) -> Any:
        hello = {"profile": "gitdir", "pins": self._pins(path).to_json()}
        return self.pool.call(principal or self.principal, hello, {"op": op, "args": args})

    # -- reads
    def observe(self, path: str | os.PathLike[str], refs: Iterable[str] = (), principal: str | None = None) -> Any:
        return self._call(path, "observe", {"refs": list(refs)}, principal)

    def diff(
        self,
        path: str | os.PathLike[str],
        *,
        branch: str,
        default_branch: str | None,
        context: int = 3,
        max_bytes: int = 262144,
        principal: str | None = None,
    ) -> dict[str, Any]:
        args = {"branch": branch, "default_branch": default_branch, "context": context, "max_bytes": max_bytes}
        return self._call(path, "diff", args, principal)


def _key(path: str | os.PathLike[str]) -> str:
    """The pin store's key for a declared working copy: its absolute path, normalised **lexically**. Never
    ``realpath``: after registration the path's components are agent-writable, and resolving them would let a
    symlink planted at repo A's path select repo B's pins. A symlinked path simply finds no pin (or its own pin,
    whose O_NOFOLLOW walk then refuses)."""
    p = os.fspath(path)
    if not os.path.isabs(p) or "\0" in p:
        raise Unavailable("a working copy path must be absolute")
    return os.path.normpath(p)


def identity_of(urls: list[str], name: str) -> str:
    """F1 5.7: the canonical identity from the raw ``remote.origin.url`` values (one value, else ``local:<name>``)."""
    return canon.canonical_repo_identity(urls[0] if len(urls) == 1 else "", name)
