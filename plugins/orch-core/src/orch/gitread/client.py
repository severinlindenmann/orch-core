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
from .errors import Busy, RemoteMismatch, Unavailable
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

    def call(self, principal: str, hello: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
        """One request in a fresh child. Raises :class:`Unavailable`, :class:`RemoteMismatch` or :class:`Busy`."""
        if not isinstance(principal, str) or not principal:
            raise Unavailable("a reader request without a principal")
        deadline = time.monotonic() + self._wall
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
        ``settings.changed`` that moves a repo, which gives it a new path, leads to a new pin)."""
        out: dict[str, str | None] = {}
        reals: list[str] = []
        for p in declared:
            try:
                reals.append(_key(p))
            except Unavailable as e:
                out[os.fspath(p)] = e.reason  # an unclean path is refused, never normalised into another one
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
    ) -> dict[str, Any]:
        args = {"branch": branch, "default_branch": default_branch, "context": context, "max_bytes": max_bytes}
        return _check_diff(self._call(path, "diff", args, principal), max_bytes)

    def tree_check(self, path: str | os.PathLike[str], name: str, principal: str | None = None) -> dict[str, Any]:
        """``TreeCheckResult`` for the declared working copy at ``path`` (repo ``name``). Raises
        :class:`Unavailable` when no clean answer can be established (callers fail closed)."""
        from . import treejob

        if not self.tree_checks:
            raise Unavailable("the clean-tree check needs the worker, which this workspace does not have yet")
        job = treejob.job_input(name, self._pins(path))
        return treejob.run_child(self.pool, principal or self.principal, job)


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


_FILE_KEYS = {"path": str, "old_path": type(None), "status": str, "add": int, "del": int, "binary": bool}


def _check_diff(got: Any, max_bytes: int) -> dict[str, Any]:
    import base64
    import binascii

    if not isinstance(got, dict) or got.get("object_format") not in _HEX:
        raise Unavailable("the reader answered nonsense")
    n = _HEX[got["object_format"]]
    _sha(got.get("base_sha"), n)
    _sha(got.get("head_sha"), n)
    if got.get("base_source") not in ("local_ref", "first_parent") or type(got.get("base_moved")) is not int:
        raise Unavailable("the reader answered nonsense")
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
