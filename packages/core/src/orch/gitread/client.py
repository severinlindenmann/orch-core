"""The host side of the reader: a pool of at most four children, the pin store, and the backend of
``Capabilities.git`` in host mode (D-P2-9, D-P2-19, F7, F10).

* :class:`ReaderPool` starts every child through :func:`orch.host.launcher.spawn_gitread` (the one spawn the host's
  audit hook allows). At most :data:`~orch.gitread.limits.MAX_CHILDREN` run at once host-wide, and one per
  principal: the rest queue (bounded: over the bound, ``quota.host``), with the 30 s deadline counted from when the
  request is queued. A child that runs past it, answers more than 16 MiB or goes over 512 MiB resident is killed with
  its process group and the request is ``observe.unavailable``. Every answer is checked for shape (object ids of the
  repo's format, field types) before anything of it reaches the caller.
* :class:`PinStore` keeps the pins of each declared working copy, by its declared path, in memory and (when given a
  file) in the host's state directory, written atomically with mode 0600.
* :class:`HostGitReader` is what ``Capabilities.host(..., git=...)`` gets. :meth:`HostGitReader.sync` pins the repos
  that appear in ``settings.repos`` (registration, and a ``settings.changed`` that adds or moves one; an existing pin
  is **never** replaced for the same path) and drops the ones that disappeared. The new ones are pinned up to
  :data:`~orch.gitread.limits.PIN_BATCH` at a time in one child (``pin_many``, #471), not one child per repo.
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
from .errors import Busy, NotFound, RemoteMismatch, Unavailable
from .fs import RepoPins

__all__ = ["HostGitReader", "PinStore", "ReaderPool"]


def _rss_reader() -> Callable[[int], int | None]:
    """A function giving a child's resident memory in bytes, built **at import** (the host's audit hook refuses
    ``ctypes.dlopen``/``dlsym`` and opens outside its roots afterwards, and :func:`orch.host.audit.warm` imports this
    module before the hook). macOS: ``proc_pid_rusage`` (the larger of resident size and physical footprint). Linux:
    ``/proc/<pid>/statm`` through a ``/proc`` fd opened here (relative, ``O_NOFOLLOW`` opens below it). Elsewhere, or
    when unavailable: ``None`` (Linux children also have ``RLIMIT_AS``)."""
    import sys

    if sys.platform == "darwin":
        try:
            import ctypes

            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            fn = lib.proc_pid_rusage
            fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            fn.restype = ctypes.c_int

            class _Info(ctypes.Structure):  # struct rusage_info_v0
                _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
                    (n, ctypes.c_uint64)
                    for n in (
                        "user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size resident_size "
                        "phys_footprint proc_start_abstime proc_exit_abstime"
                    ).split()
                ]

            def darwin(pid: int) -> int | None:
                info = _Info()
                if fn(pid, 0, ctypes.byref(info)) != 0:
                    return None
                return max(info.resident_size, info.phys_footprint)

            return darwin
        except (OSError, AttributeError):
            return lambda pid: None
    try:
        proc_fd = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return lambda pid: None
    page = os.sysconf("SC_PAGE_SIZE")
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    def linux(pid: int) -> int | None:
        try:
            d = os.open(str(pid), os.O_RDONLY | os.O_DIRECTORY | nofollow | os.O_CLOEXEC, dir_fd=proc_fd)
            try:
                f = os.open("statm", os.O_RDONLY | nofollow | os.O_CLOEXEC, dir_fd=d)
            finally:
                os.close(d)
            try:
                return int(os.read(f, 256).split()[1]) * page
            finally:
                os.close(f)
        except (OSError, ValueError, IndexError):
            return None

    return linux


_rss = _rss_reader()


class ReaderPool:
    """At most ``max_children`` children at once host-wide and one per principal (F10). A request waits at most
    until its own deadline, which starts **when it is queued** (30 s in all, waiting included); a principal with
    :data:`~orch.gitread.limits.QUEUE_PER_PRINCIPAL` requests waiting, or a host with
    :data:`~orch.gitread.limits.QUEUE_MAX`, is answered :class:`~orch.gitread.errors.Busy` (``quota.host``) at once.
    While a child runs, the host polls its resident memory and kills its process group above ``rss_max``."""

    def __init__(
        self,
        *,
        max_children: int = limits.MAX_CHILDREN,
        spawn: Callable[[], Any] | None = None,
        wall: float = limits.WALL_SECONDS,
        rss_max: int = limits.RSS_MAX,
        queue_per_principal: int = limits.QUEUE_PER_PRINCIPAL,
        queue_max: int = limits.QUEUE_MAX,
    ) -> None:
        if spawn is None:
            from orch.host.launcher import spawn_gitread

            spawn = spawn_gitread
        self._spawn = spawn
        self._wall = wall
        self._rss_max = rss_max
        self._queue_per_principal = queue_per_principal
        self._queue_max = queue_max
        self._slots = threading.BoundedSemaphore(max_children)
        self._mu = threading.Lock()
        self._principals: dict[str, list[Any]] = {}  # principal -> [lock, requests queued or running]
        self.waiting = 0
        self.active = 0
        self.peak = 0
        self.started = 0

    def call(
        self, principal: str, hello: dict[str, Any], req: dict[str, Any], *, wall: float | None = None
    ) -> dict[str, Any]:
        """One request in a fresh child. Raises :class:`Unavailable`, :class:`RemoteMismatch` or :class:`Busy`.
        ``wall``: a shorter deadline than the pool's (a caller's remaining budget), counted from now."""
        if not isinstance(principal, str) or not principal:
            raise Unavailable("a reader request without a principal")
        deadline = time.monotonic() + (self._wall if wall is None else max(0.0, min(self._wall, wall)))
        with self._mu:
            entry = self._principals.get(principal)
            if (entry is not None and entry[1] >= self._queue_per_principal) or self.waiting >= self._queue_max:
                raise Busy("too many git reads are waiting; retry shortly")
            entry = self._principals.setdefault(principal, [threading.Lock(), 0])
            entry[1] += 1
            self.waiting += 1
        waiting = True
        try:
            if not entry[0].acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise Busy(f"no git reader child within {self._wall:g} s")
            try:
                if not self._slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
                    raise Busy(f"no git reader child within {self._wall:g} s")
                try:
                    with self._mu:
                        self.waiting -= 1
                        waiting = False
                        self.active += 1
                        self.peak = max(self.peak, self.active)
                        self.started += 1
                    try:
                        resp = self._run(hello, req, deadline)
                    finally:
                        with self._mu:
                            self.active -= 1
                finally:
                    self._slots.release()
            finally:
                entry[0].release()
        finally:
            with self._mu:
                if waiting:
                    self.waiting -= 1
                entry[1] -= 1
                if entry[1] == 0 and self._principals.get(principal) is entry:
                    del self._principals[principal]
        if "ok" in resp and isinstance(resp["ok"], dict):
            return resp["ok"]
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        message = str(err.get("message") or "the reader failed")[:300]
        if err.get("code") == RemoteMismatch.code:
            raise RemoteMismatch(message)
        if err.get("code") == NotFound.code:
            raise NotFound(message)
        raise Unavailable(message)

    def _run(self, hello: dict[str, Any], req: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
        data = (json.dumps(hello) + "\n" + json.dumps(req) + "\n").encode("utf-8")
        if len(data) > 2 * limits.REQUEST_MAX:
            raise Unavailable("the request is too large")
        if deadline is None:
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
                    rss = _rss(proc.pid)
                    if rss is not None and rss > self._rss_max:
                        killed = self._kill(proc)
                        raise Unavailable(f"the reader went over its memory limit ({self._rss_max >> 20} MiB)")
                    if not sel.select(min(left, 0.1)):
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
        #: H11d (D-P2-32): ``{ticket uid: {repo: {"gen": int, "reg": registration | None}}}``. The generation of a
        #: (ticket, repo) only ever grows: every create and every removal bumps it, and the slot is kept after a
        #: removal so the next registration can never reuse an old generation
        self._worktrees: dict[str, dict[str, dict[str, Any]]] = {}
        self._mu = threading.Lock()
        if path is not None and path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("repos"), dict):
                raise ValueError(f"{path}: not a pin file")
            self._pins = {k: RepoPins.from_json(v) for k, v in doc["repos"].items()}
            self._worktrees = _load_worktrees(doc.get("worktrees", {}))

    def get(self, real: str) -> RepoPins | None:
        with self._mu:
            return self._pins.get(real)

    def paths(self) -> list[str]:
        with self._mu:
            return sorted(self._pins)

    def update(self, put: dict[str, RepoPins], drop: Iterable[str]) -> None:
        """Add and drop pins: the file first (a fresh 0600 temporary file, fsynced, renamed over it), then memory, so
        a pin that could not be written is never held in memory only (it would be taken afresh after a restart).
        Raises ``OSError`` when the file cannot be written; nothing changes then."""
        with self._mu:
            pins = {k: v for k, v in self._pins.items() if k not in set(drop)}
            pins.update(put)
            self._write(pins, self._worktrees)
            self._pins = pins

    def _write(self, pins: dict[str, RepoPins], worktrees: dict[str, dict[str, dict[str, Any]]]) -> None:
        if self.path is None:
            return
        doc: dict[str, Any] = {"v": 1, "repos": {k: v.to_json() for k, v in sorted(pins.items())}}
        if worktrees:
            doc["worktrees"] = worktrees
        tmp = self.path.with_name(self.path.name + ".tmp")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)  # a leftover is never reused (its mode or owner could be another)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(doc, sort_keys=True))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- H11d: worktree registrations (D-P2-32), ``worktrees.<ticket uid>.<repo>`` of the same file
    def worktree(self, uid: str, repo: str) -> dict[str, Any]:
        """The slot ``{"gen": int, "reg": registration | None}`` of (ticket, repo); generation 0 and no registration
        when there never was one. A copy: changing it changes nothing."""
        with self._mu:
            slot = (self._worktrees.get(uid) or {}).get(repo)
            return json.loads(json.dumps(slot)) if slot is not None else {"gen": 0, "reg": None}

    def worktrees(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Every (ticket uid, repo, slot) with a registration."""
        with self._mu:
            return [
                (uid, repo, json.loads(json.dumps(slot)))
                for uid, repos in sorted(self._worktrees.items())
                for repo, slot in sorted(repos.items())
                if slot.get("reg") is not None
            ]

    def set_worktree(self, uid: str, repo: str, reg: dict[str, Any] | None, *, expect: int | None = None) -> int:
        """Register (``reg``) or remove (``None``) the worktree of (ticket, repo) and bump its generation; the file is
        written first, then memory. ``expect``: the generation the caller read (compare-and-swap); another one raises
        :class:`ValueError` and nothing changes. Returns the new generation."""
        if reg is not None:
            _check_reg(reg)
        with self._mu:
            cur = (self._worktrees.get(uid) or {}).get(repo) or {"gen": 0, "reg": None}
            if expect is not None and cur["gen"] != expect:
                raise ValueError("the worktree registration changed meanwhile")
            gen = int(cur["gen"]) + 1
            worktrees = json.loads(json.dumps(self._worktrees))
            worktrees.setdefault(uid, {})[repo] = {"gen": gen, "reg": reg}
            self._write(self._pins, worktrees)
            self._worktrees = worktrees
            return gen

    def worktree_pins(self, path: str) -> RepoPins | None:
        """The pins of the registered worktree whose path is exactly ``path`` (a registration without pins: none)."""
        with self._mu:
            for repos in self._worktrees.values():
                for slot in repos.values():
                    reg = slot.get("reg")
                    if reg is not None and reg.get("path") == path and reg.get("pins") is not None:
                        return RepoPins.from_json(reg["pins"])
        return None


class HostGitReader:
    """The host-mode backend of ``Capabilities.git``: every read is a fresh child under the ``gitdir`` profile.

    ``tree_checks``: whether :meth:`tree_check` runs here. Tier A (phase 1, same UID, no worker): ``True``, the host
    runs the ``tree.check`` job in a ``worktree``-profile child itself (:mod:`orch.gitread.treejob`). Tier B: ``False``
    until the phase-3 worker takes the job over, so the check is unknown and its callers fail closed."""

    def __init__(
        self, pool: ReaderPool, pins: PinStore | None = None, *, principal: str = "host", tree_checks: bool = False
    ) -> None:
        self.pool = pool
        self.pins = pins if pins is not None else PinStore()
        self.principal = principal
        self.tree_checks = tree_checks

    # -- pins
    def sync(self, declared: Iterable[str | os.PathLike[str]]) -> dict[str, str | None]:
        """Pin every declared working copy not pinned yet; drop pins of paths no longer declared. Returns
        ``{real path: None | why it could not be pinned}``. An existing pin is kept as it is (only a
        ``settings.changed`` that moves a repo, which gives it a new path, leads to a new pin). The repos not pinned
        yet are pinned in one child per :data:`~orch.gitread.limits.PIN_BATCH` of them (:meth:`_pin_batch`)."""
        out: dict[str, str | None] = {}
        reals: list[str] = []
        for p in declared:
            try:
                reals.append(_key(p))
            except Unavailable as e:
                out[os.fspath(p)] = e.reason  # an unclean path is refused, never normalised into another one
        put: dict[str, RepoPins] = {}
        todo: list[str] = []
        for real in reals:
            if self.pins.get(real) is not None:
                out[real] = None
            elif real not in todo:
                todo.append(real)
        hello = {"profile": "pin", "roots": reals}
        for i in range(0, len(todo), limits.PIN_BATCH):
            for real, got in self._pin_batch(hello, todo[i : i + limits.PIN_BATCH]).items():
                if isinstance(got, RepoPins):
                    put[real] = got
                    out[real] = None
                else:
                    out[real] = got
        drop = [p for p in self.pins.paths() if p not in reals]
        # one directory is one repo: two declared paths whose working copy, gitdir or commondir is the same
        # (device, inode) (a case or Unicode variant on APFS, a hard link, a bind mount, a borrowed object store)
        # are refused, never pinned twice. The one sharing allowed is git's worktree layout, verified at pin time:
        # a linked worktree's commondir is a main repository's gitdir (and its sibling worktrees' commondir).
        keep = {p: self.pins.get(p) for p in self.pins.paths() if p not in drop}
        for real in list(put):
            mine = put[real]
            for other, theirs in [*keep.items(), *((k, v) for k, v in put.items() if k != real)]:
                if theirs is None:
                    continue
                if _clash(mine, theirs):
                    out[real] = f"the same directory as the declared repo at {other}"
                    del put[real]
                    break
        if put or drop:
            self.pins.update(put, drop)
        return out

    def _pin_one(self, hello: dict[str, Any], real: str) -> RepoPins | str:
        try:
            got = self.pool.call(self.principal, hello, {"op": "pin", "args": {"path": real}})
            return _check_layout(RepoPins.from_json(got.get("pins")), real, hello["roots"])
        except Unavailable as e:
            return e.reason

    def _pin_batch(self, hello: dict[str, Any], paths: list[str]) -> dict[str, RepoPins | str]:
        """``{path: its pins | why not}`` for ``paths`` (at most :data:`~orch.gitread.limits.PIN_BATCH`), pinned in
        **one** child (``pin_many``, #471) under the same ``pin`` profile, roots and checks as one child per path.
        A batch that fails as a whole (the child dies, overruns its limits or answers anything but one well-formed
        result per path) decides nothing: each of its paths is pinned again in its own child, so one repo that
        breaks the batch never poisons the others. A busy pool (``quota.host``) is not retried path by path: every
        path is refused with that reason and retried after the caller's backoff."""
        if len(paths) == 1:
            return {paths[0]: self._pin_one(hello, paths[0])}
        try:
            got = self.pool.call(self.principal, hello, {"op": "pin_many", "args": {"paths": paths}})
            return dict(zip(paths, _check_pin_many(got, paths, hello["roots"]), strict=True))
        except Busy as e:
            return dict.fromkeys(paths, e.reason)
        except Unavailable:
            return {p: self._pin_one(hello, p) for p in paths}

    def _pins(self, path: str | os.PathLike[str]) -> RepoPins:
        key = _key(path)
        pins = self.pins.get(key)
        if pins is None:
            pins = self.pins.worktree_pins(key)  # H11d: a registered worktree, by its host-chosen path
        if pins is None:
            raise Unavailable("the repository was not pinned at registration")
        return pins

    def pin_worktree(
        self, declared: str | os.PathLike[str], path: str, branch: str, principal: str | None = None
    ) -> RepoPins:
        """H11d (§12.7): pin the linked worktree the runner created at ``path`` for the declared repo at ``declared``.
        Its ``.git`` file must name ``<C>/worktrees/<id>`` with ``<C>`` the declared repo's **pinned** commondir
        (device and inode compared), the back-pointer must name this worktree, and ``HEAD`` must be exactly
        ``ref: refs/heads/<branch>``. Raises :class:`Unavailable` (the registration is then ``unpinned``)."""
        dp = self.pins.get(_key(declared))
        if dp is None:
            raise Unavailable("the declared repository is not pinned")
        wt = _key(path)
        common = dp.commondir.path
        if not common.endswith("/.git") or len(common) <= len("/.git"):
            raise Unavailable("the declared repository's commondir is not <repo>/.git")
        root = common[: -len("/.git")]
        hello = {"profile": "pin", "roots": [wt, root]}
        got = self.pool.call(principal or self.principal, hello, {"op": "pin", "args": {"path": wt}})
        pins = RepoPins.from_json(got.get("pins"))
        if _id(pins.commondir) != _id(dp.commondir) or pins.commondir.path != common:
            raise Unavailable("the worktree belongs to another repository than the declared one")
        if _id(pins.gitdir) == _id(dp.gitdir) or _id(pins.worktree) == _id(dp.worktree):
            raise Unavailable("the worktree is the declared working copy itself")
        look = self.pool.call(
            principal or self.principal,
            {"profile": "gitdir", "pins": pins.to_json()},
            {"op": "observe", "args": {"refs": []}},
        )
        if _check_observe(look, []).get("head_ref") != "refs/heads/" + branch:
            raise Unavailable("the worktree's HEAD is not the ticket's branch")
        return pins

    def verify_worktree(self, path: str, principal: str | None = None) -> None:
        """H11d: the registered worktree at ``path`` is still the one that was pinned: its ``.git`` file, walked again
        from the pinned commondir's root, names the same gitdir and commondir (device and inode), and the directory is
        the same one. A re-pointed ``.git`` file, a swapped directory or a moved worktree is :class:`Unavailable`."""
        wt = _key(path)
        pins = self.pins.worktree_pins(wt)
        if pins is None:
            raise Unavailable("the worktree is not registered")
        common = pins.commondir.path
        if not common.endswith("/.git") or len(common) <= len("/.git"):
            raise Unavailable("the worktree's commondir is not <repo>/.git")
        hello = {"profile": "pin", "roots": [wt, common[: -len("/.git")]]}
        try:
            got = self.pool.call(principal or self.principal, hello, {"op": "pin", "args": {"path": wt}})
            now = RepoPins.from_json(got.get("pins"))
        except Unavailable:
            raise Unavailable("the worktree no longer resolves to the pinned repository") from None
        same = (
            _id(now.worktree) == _id(pins.worktree)
            and _id(now.gitdir) == _id(pins.gitdir)
            and _id(now.commondir) == _id(pins.commondir)
            and now.gitdir.path == pins.gitdir.path
        )
        if not same:
            raise Unavailable("the worktree's .git file or location changed since it was registered")

    def _call(
        self,
        path: str | os.PathLike[str],
        op: str,
        args: dict[str, Any],
        principal: str | None,
        wall: float | None = None,
    ) -> Any:
        hello = {"profile": "gitdir", "pins": self._pins(path).to_json()}
        if wall is None:
            return self.pool.call(principal or self.principal, hello, {"op": op, "args": args})
        return self.pool.call(principal or self.principal, hello, {"op": op, "args": args}, wall=wall)

    # -- reads
    def observe(self, path: str | os.PathLike[str], refs: Iterable[str] = (), principal: str | None = None) -> Any:
        refs = list(refs)
        return _check_observe(self._call(path, "observe", {"refs": refs}, principal), refs)

    def diff(
        self,
        path: str | os.PathLike[str],
        *,
        branch: str,
        default_branch: str | None,
        context: int = 3,
        max_bytes: int = 262144,
        principal: str | None = None,
        wall: float | None = None,
        ext: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = {"branch": branch, "default_branch": default_branch, "context": context, "max_bytes": max_bytes}
        if ext:
            args["ext"] = ext
        return _check_diff(self._call(path, "diff", args, principal, wall), max_bytes)

    def tree_check(
        self, path: str | os.PathLike[str], name: str, principal: str | None = None, *, removal: bool = False
    ) -> dict[str, Any]:
        """``TreeCheckResult`` for the declared working copy at ``path`` (repo ``name``). Raises
        :class:`Unavailable` when no clean answer can be established (callers fail closed)."""
        from . import treejob

        if not self.tree_checks:
            raise Unavailable("the clean-tree check needs the worker, which this workspace does not have yet")
        job = treejob.job_input(name, self._pins(path))
        return treejob.run_child(self.pool, principal or self.principal, job, removal=removal)

    def worktree_diff(
        self,
        path: str | os.PathLike[str],
        name: str,
        principal: str | None = None,
        *,
        max_bytes: int = 262144,
        max_files: int = 200,
    ) -> dict[str, Any]:
        """The uncommitted changes of the registered working copy at ``path`` as files plus unified text (focus mode
        P2-5), read in a ``worktree``-profile child. Raises :class:`Unavailable` like :meth:`tree_check`."""
        from . import treejob

        if not self.tree_checks:
            raise Unavailable("the live diff needs the worker, which this workspace does not have yet")
        job = treejob.job_input(name, self._pins(path))
        hello = {"profile": "worktree", "pins": treejob.pins_of(job).to_json()}
        req = {"op": "worktree_diff", "args": {"max_bytes": max_bytes, "max_files": max_files}}
        return _check_worktree_diff(self.pool.call(principal or self.principal, hello, req), max_bytes, max_files)


def _id(pin: Any) -> tuple[int, int]:
    return (pin.dev, pin.ino)


def _clash(a: RepoPins, b: RepoPins) -> bool:
    """Whether two pinned repos share a directory in a way git's layout does not allow."""
    if _id(a.worktree) == _id(b.worktree) or _id(a.gitdir) == _id(b.gitdir):
        return True
    a_linked, b_linked = _id(a.gitdir) != _id(a.commondir), _id(b.gitdir) != _id(b.commondir)
    shared = {_id(a.commondir), _id(a.gitdir)} & {_id(b.commondir), _id(b.gitdir)}
    if not shared:
        return False
    if a_linked and b_linked:  # two worktrees of one main repository
        return _id(a.commondir) != _id(b.commondir) or bool(shared - {_id(a.commondir)})
    if a_linked:  # a is a worktree of main repo b
        return shared != {_id(b.gitdir)} or _id(a.commondir) != _id(b.gitdir)
    if b_linked:
        return shared != {_id(a.gitdir)} or _id(b.commondir) != _id(a.gitdir)
    return True


_HEX = {"sha1": 40, "sha256": 64}


def _sha(v: Any, n: int, *, null: bool = False) -> str | None:
    if v is None and null:
        return None
    if not isinstance(v, str) or len(v) != n or any(c not in "0123456789abcdef" for c in v):
        raise Unavailable("the reader answered a malformed object id")
    return v


def _check_observe(got: Any, refs: list[str]) -> dict[str, Any]:
    """The child's ``observe`` answer, checked before anything of it can reach a signed source list (review 7)."""
    from .refs import check_refname

    if not isinstance(got, dict) or got.get("object_format") not in _HEX:
        raise Unavailable("the reader answered nonsense")
    n = _HEX[got["object_format"]]
    head_ref = got.get("head_ref")
    if head_ref is not None and (
        not isinstance(head_ref, str) or not head_ref.startswith("refs/heads/") or not check_refname(head_ref)
    ):
        raise Unavailable("the reader answered a malformed HEAD")
    if not isinstance(got.get("head_error"), str | type(None)):
        raise Unavailable("the reader answered nonsense")
    _sha(got.get("head_sha"), n, null=True)
    r = got.get("refs")
    if not isinstance(r, dict) or set(r) != set(refs):
        raise Unavailable("the reader answered other refs than asked")
    for v in r.values():
        _sha(v, n, null=True)
    urls = got.get("origin_urls")
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise Unavailable("the reader answered nonsense")
    return got


def _check_layout(pins: RepoPins, real: str, roots: list[str]) -> RepoPins:
    """The pins a child answered for ``real`` must have the shape :func:`orch.gitread.pinning.pin_repo` gives: the
    working copy is ``real`` itself, the commondir is ``<declared repo>/.git`` and the gitdir is the commondir (a main
    repository, then ``real/.git``) or ``<commondir>/worktrees/<id>`` of another declared repo (a linked worktree).
    A child that forged pins (a path outside the declared repos for the later ``gitdir`` profile to open) is refused
    here, whichever repo of a batch it answered for (#471: one child answers for many repos)."""
    gits = {r.rstrip("/") + "/.git" for r in roots}
    own = real.rstrip("/") + "/.git"
    wt, gd, cd = pins.worktree.path, pins.gitdir.path, pins.commondir.path
    main = gd == cd == own
    linked = (
        cd in gits
        and cd != own
        and gd.startswith(cd + "/worktrees/")
        and gd.count("/") == cd.count("/") + 2
        and gd.rpartition("/")[2] not in ("", ".", "..")
    )
    if wt != real or not (main or linked):
        raise Unavailable("the reader answered pins outside the declared repos")
    return pins


def _check_pin_many(got: Any, paths: list[str], roots: list[str]) -> list[RepoPins | str]:
    """The child's ``pin_many`` answer, checked as a whole before any of it is used: one result per path, each either
    pins of git's layout for that path (:func:`_check_layout`) or an error with a string message. Anything else is
    :class:`Unavailable` (the caller then pins each path in its own child)."""
    results = got.get("results") if isinstance(got, dict) and set(got) == {"results"} else None
    if not isinstance(results, list) or len(results) != len(paths):
        raise Unavailable("the reader answered nonsense")
    out: list[RepoPins | str] = []
    for path, r in zip(paths, results, strict=True):
        if isinstance(r, dict) and set(r) == {"pins"}:
            out.append(_check_layout(RepoPins.from_json(r["pins"]), path, roots))
            continue
        err = r.get("error") if isinstance(r, dict) and set(r) == {"error"} else None
        if not isinstance(err, dict) or set(err) != {"code", "message"} or not isinstance(err["message"], str):
            raise Unavailable("the reader answered nonsense")
        out.append(err["message"][:300] or "the reader failed")
    return out


def _check_worktree_diff(got: Any, max_bytes: int, max_files: int) -> dict[str, Any]:
    if not isinstance(got, dict) or set(got) != {"head", "head_ref", "files", "unified", "truncated"}:
        raise Unavailable("the reader answered nonsense")
    if got["head"] is not None and not isinstance(got["head"], str):
        raise Unavailable("the reader answered nonsense")
    if not isinstance(got["unified"], str) or len(got["unified"].encode()) > 4 * max_bytes:
        raise Unavailable("the reader answered nonsense")
    files = got["files"]
    if type(got["truncated"]) is not bool or not isinstance(files, list) or len(files) > max_files:
        raise Unavailable("the reader answered nonsense")
    for f in files:
        ok = isinstance(f, dict) and {"path", "status", "added", "removed", "binary"} <= set(f) <= {
            "path", "status", "added", "removed", "binary", "too_large"}  # fmt: skip
        if not ok or not isinstance(f["path"], str) or f["status"] not in ("M", "A", "D", "?"):
            raise Unavailable("the reader answered a malformed file entry")
        if type(f["added"]) is not int or type(f["removed"]) is not int or type(f["binary"]) is not bool:
            raise Unavailable("the reader answered a malformed file entry")
    return got


_BASE_SOURCES = ("local_ref", "first_parent", "host_pinned", "approved", "pushed")
_FILE_KEYS = {"path": str, "old_path": type(None), "status": str, "add": int, "del": int, "binary": bool}


def _check_diff(got: Any, max_bytes: int) -> dict[str, Any]:
    import base64
    import binascii

    if not isinstance(got, dict) or got.get("object_format") not in _HEX:
        raise Unavailable("the reader answered nonsense")
    n = _HEX[got["object_format"]]
    _sha(got.get("base_sha"), n)
    _sha(got.get("head_sha"), n)
    if got.get("base_source") not in _BASE_SOURCES or type(got.get("base_moved")) is not int:
        raise Unavailable("the reader answered nonsense")
    _sha(got.get("observed_base"), n, null=True)
    commits = got.get("commits")
    if not isinstance(commits, list) or len(commits) > limits.COMMITS_SHOWN or type(got.get("commits_more")) is not int:
        raise Unavailable("the reader answered nonsense")
    for c in commits:
        if not isinstance(c, dict) or set(c) != {"sha", "subject"} or not isinstance(c["subject"], str):
            raise Unavailable("the reader answered nonsense")
        _sha(c["sha"], n)
    files = got.get("files")
    if not isinstance(files, list) or len(files) > limits.DIFF_FILES_MAX:
        raise Unavailable("the reader answered nonsense")
    for f in files:
        if not isinstance(f, dict) or set(f) != {*_FILE_KEYS, "too_large"} or type(f["too_large"]) is not bool:
            raise Unavailable("the reader answered a malformed file entry")
        if any(type(f[k]) is not t for k, t in _FILE_KEYS.items()) or f["status"] not in ("A", "M", "D", "T"):
            raise Unavailable("the reader answered a malformed file entry")
    if not isinstance(got.get("patch"), str) or type(got.get("truncated")) is not bool:
        raise Unavailable("the reader answered nonsense")
    try:
        if len(base64.b64decode(got["patch"], validate=True)) > max_bytes:
            raise Unavailable("the reader answered more than max_bytes")
    except (binascii.Error, ValueError):
        raise Unavailable("the reader answered a malformed patch") from None
    urls = got.get("origin_urls")
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise Unavailable("the reader answered nonsense")
    return got


def _key(path: str | os.PathLike[str]) -> str:
    """The pin store's key for a declared working copy: the path **byte for byte** as the host computed it once from
    the owner-signed settings (``Git.repo_path``), and it must be clean (absolute, no ``.``, ``..``, empty component
    or trailing slash). Never resolved, normalised or case-folded again: a variant (``a/``, ``a/.``, ``x/../a``,
    ``a//b``, another case or Unicode form) is refused or finds no pin; it can never find another repo's pin. Repos
    themselves are told apart by the (device, inode) of their pins (:meth:`HostGitReader.sync`)."""
    from .fs import abs_parts

    p = os.fspath(path)
    abs_parts(p)
    return p


def identity_of(urls: list[str], name: str) -> str:
    """F1 5.7: the canonical identity from the raw ``remote.origin.url`` values (one value, else ``local:<name>``)."""
    return canon.canonical_repo_identity(urls[0] if len(urls) == 1 else "", name)


_REG_KEYS = {"path", "branch", "pins", "created_at", "created_by", "idem"}


def _check_reg(reg: Any) -> None:
    if not isinstance(reg, dict) or set(reg) != _REG_KEYS:
        raise ValueError("malformed worktree registration")
    if not isinstance(reg["path"], str) or not reg["path"].startswith("/") or not isinstance(reg["branch"], str):
        raise ValueError("malformed worktree registration")
    if reg["pins"] is not None:
        try:
            RepoPins.from_json(reg["pins"])
        except Unavailable:
            raise ValueError("malformed worktree registration pins") from None
    if not isinstance(reg["created_at"], str) or not isinstance(reg["created_by"], dict):
        raise ValueError("malformed worktree registration")
    if reg["idem"] is not None and not isinstance(reg["idem"], str):
        raise ValueError("malformed worktree registration")


def _load_worktrees(doc: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """The ``worktrees`` section of a pin file, checked; anything malformed refuses the whole file (fail closed)."""
    if not isinstance(doc, dict):
        raise ValueError("not a pin file: worktrees")
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for uid, repos in doc.items():
        if not isinstance(uid, str) or not isinstance(repos, dict):
            raise ValueError("not a pin file: worktrees")
        for repo, slot in repos.items():
            if not isinstance(repo, str) or not isinstance(slot, dict) or set(slot) != {"gen", "reg"}:
                raise ValueError("not a pin file: worktrees")
            if type(slot["gen"]) is not int or slot["gen"] < 0:
                raise ValueError("not a pin file: worktrees")
            if slot["reg"] is not None:
                _check_reg(slot["reg"])
            out.setdefault(uid, {})[repo] = {"gen": slot["gen"], "reg": slot["reg"]}
    return out
