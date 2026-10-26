"""Run supervision (D-P2-29, D-P2-37, D-P2-39): spawn through the trampoline, PTY and ring, stop, sweeps, cleanup.

**A run is its session.** The trampoline (:mod:`orch.runner.exec`) makes the run's first process a session leader
with the PTY as controlling terminal; its PID is the run's pid, process group and session id. The runner keeps the
PTY master, reads it continuously into a 1 MiB :class:`~orch.runner.ring.Ring` (a slow or absent viewer never blocks a
run) and reaps the leader.

**Stop** (``stop`` and the clean-up after a leader exits): ``SIGHUP`` and ``SIGTERM`` to every member, a grace period,
``SIGKILL`` to what is left, then a check. Members are found per platform:

* Linux with a delegated cgroup (:mod:`orch.runner.cgroup`): the run's cgroup, joined by the trampoline before exec;
  the kill is ``cgroup.kill``; ``complete`` means nothing is left in the cgroup and nothing was seen leaving it (a
  same-UID run can leave a writable delegated cgroup, so this too is not containment).
* macOS, and Linux without delegation: the processes in the run's process group or session that were born after the
  leader (only while the run is live in this runner's lifetime), plus every descendant the escape scan saw. This is
  **best-effort cleanup, not containment**: a descendant that calls ``setsid()`` leaves the session. The scan (every
  ``scan_interval`` seconds and right before a stop) follows parent links from the run's processes; a descendant found
  outside the session marks the run ``escaped`` and is tracked by birth identity. A run that ever escaped reports
  ``cleanup: "incomplete"`` even when every tracked process is gone, because a descendant that escaped between two
  scans cannot be seen.

**Never a reused PID.** Every signal goes through :func:`orch.runner.procs.signal_verified` with the recorded birth
time; the session and group sweep is skipped when the session leader's PID now belongs to a process with another
birth time. An ended or crash-swept record never matches a session or a group at all: only exact identities (the
leader, the escapees and the members saved at the last scan), and only on the boot the record was made on. Never by
name, never UID-wide, never a group signal. Processes of every UID count as members; only this runner's UID is
signalled, and a member of another UID (``sudo``, a setuid helper) makes the clean-up ``incomplete``.

**Persisted first.** The run record is written (state ``spawning``) before the process exists, and a ``spawn`` whose
run id is already recorded never starts a second process: a lost ack, a host reconnect or a host restart between
intent and ack gives exactly one process. After a runner crash, :meth:`Supervisor.open` marks every run still live as
``lost`` with ``cleanup: "unknown"`` and sweeps the recorded processes that still match their birth identity.

**Run end** is the leader's exit; the PTY is then drained for at most 2 s and the master closed (never waiting for EOF,
which a background process holding the slave would delay forever); the remaining members get the stop sequence.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import select
import selectors
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from orch import fdwait

from . import procs
from .cgroup import CgroupTree, detect
from .ring import RING_BYTES, Ring
from .table import KEEP_SECONDS, RUN_ID, RunTable, now_iso

__all__ = ["EXEC", "SpawnError", "SpawnSpec", "Supervisor"]

LOG = logging.getLogger(__name__)

#: the trampoline, run as ``python -I <EXEC> SPEC_FD STATUS_FD`` (``-I`` implies ``-P``: no script dir on the path)
EXEC = Path(__file__).with_name("exec.py")
SPAWN_TIMEOUT = 10.0
DRAIN_SECONDS = 2.0
KILL_WAIT = 2.0
STOP_GRACE = 10.0
#: how long close() waits for the I/O thread before leaving the release of its fds to the thread itself
JOIN_SECONDS = 5.0
SCAN_SECONDS = 2.0
#: an ended run's ring is kept 1 h, for at most 64 ended runs (§9.1)
KEEP_RING_SECONDS = 3600.0
KEEP_RINGS = 64
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
#: never in a run's environment (§12.4.3 deny-list): loader, interpreter and shell start-up hooks
#: used with ``fullmatch``: prefix matches only for the families, exact names for the rest (§12.4.3)
ENV_DENY = re.compile(
    r"(?:LD_|DYLD_|PYTHON|GIT_|BASH_FUNC_).*|BASH_ENV|ENV|ZDOTDIR|IFS|PS4|NODE_OPTIONS|PERL5OPT|RUBYOPT"
)
#: never in a shell's environment: a shell acts as the person, never as an agent (D-P2-27)
SHELL_DENY = frozenset({"ORCH_GRANT", "ORCH_SESSION"})
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def run_time(run: str) -> float:
    """The creation time a run id (``r_`` + ULID) carries, in seconds."""
    ms = 0
    for ch in run[2:12]:
        ms = ms * 32 + _CROCKFORD.index(ch)
    return ms / 1000


Cleanup = Literal["complete", "incomplete", "unknown"]


class RunnerBusy(Exception):
    """Another runner serves this state directory."""


def read_umask(directory: Path, uid: int) -> int:
    """The umask ``orch runner enable`` captured (``<state>/runner/settings.json``), else 022."""
    from orch.host.custody import read_json
    from orch.host.errors import HostError

    try:
        doc = read_json(Path(directory) / "settings.json", uid) or {}
    except HostError:
        doc = {}
    value = doc.get("umask") if isinstance(doc, dict) else None
    return value if isinstance(value, int) and 0 <= value <= 0o777 else 0o022


class SpawnError(Exception):
    def __init__(self, code: str, message: str, cleanup: str = "complete") -> None:
        super().__init__(message)
        self.code, self.message = code, message
        #: what became of a process forked for the failed spawn: ``complete`` (none left), else it stays tracked
        self.cleanup = cleanup


@dataclass(frozen=True)
class SpawnSpec:
    run: str
    workspace_id: str
    kind: str
    argv: list[str]
    env: dict[str, str]
    cwd: dict[str, Any]
    cols: int = 120
    rows: int = 40
    #: ``(workspace root, work folder)`` a Claude Code trust entry was recorded for (re-checked when the run exits)
    trusted: tuple[str, str] | None = field(default=None, compare=False)

    def check(self) -> None:
        def bad(what: str) -> SpawnError:
            return SpawnError("invalid.input", f"spawn: invalid {what}")

        if not RUN_ID.fullmatch(self.run):
            raise bad("run id")
        if not re.fullmatch(r"[0-9a-f]{32}", self.workspace_id):
            raise bad("workspace id")
        if self.kind not in ("agent", "shell"):
            raise bad("kind")
        if not (1 <= len(self.argv) <= 64) or not all(isinstance(a, str) and "\0" not in a for a in self.argv):
            raise bad("argv")
        if not self.argv[0].startswith("/"):
            raise bad("argv[0] (not absolute)")
        if len(self.env) > 128 or not all(
            ENV_NAME.fullmatch(k) and isinstance(v, str) and "\0" not in v for k, v in self.env.items()
        ):
            raise bad("environment")
        denied = [k for k in self.env if ENV_DENY.fullmatch(k) or (self.kind == "shell" and k in SHELL_DENY)]
        if denied:  # defence in depth: the host's allow-list (H11a2, H11c) should never send these
            raise bad(f"environment name {denied[0][:40]}")
        cwd = self.cwd
        if not (
            isinstance(cwd, dict)
            and isinstance(cwd.get("path"), str)
            and cwd["path"].startswith("/")
            and "\0" not in cwd["path"]
            and isinstance(cwd.get("dev"), int)
            and isinstance(cwd.get("ino"), int)
        ):
            raise bad("cwd pin")
        if not (20 <= self.cols <= 500 and 5 <= self.rows <= 200):
            raise bad("size")


@dataclass
class Live:
    run: str
    proc: subprocess.Popen[bytes]
    master: int
    ring: Ring
    pid: int
    birth: int
    pgid: int
    sid: int
    cgroup: Path | None
    tracked: dict[int, int] = field(default_factory=dict)  # escapees and other out-of-session descendants
    escaped: bool = False
    exit: dict[str, Any] | None = None
    drain_until: float = 0.0
    pty_closed: bool = False
    stopping: bool = False
    cleanup: str | None = None
    #: processes of the run were still there after the last kill: retried every scan, never pruned
    lingering: bool = False
    cols: int = 120
    rows: int = 40
    #: the member set saved at the last scan (exact identities for a later sweep)
    saved: list[list[int]] = field(default_factory=list)
    #: the leader was reaped: its PID (the session and group id) may be reused, so only exact identities match
    reaped: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    ended_at: float = 0.0
    stop_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def session_ok(self) -> bool:
        """The leader's session and group id still name this run: it was neither reaped by us nor by ``Popen``
        (``returncode`` set by a ``poll``/``wait`` elsewhere means it was reaped and its PID may be reused)."""
        return not self.reaped and self.proc.returncode is None


#: ``NOTE_EXITSTATUS`` (macOS ``<sys/event.h>``): the exit event carries the wait status in ``data`` (not in ``select``)
NOTE_EXITSTATUS = 0x04000000
#: the kqueue watch is shared state on ``proc``; peeks come from the I/O thread and from stop and abort paths
_EXIT_LOCK = threading.Lock()


def watch_exit(proc: subprocess.Popen[bytes]) -> None:
    """Where there is no ``os.waitid`` (macOS), arm a kqueue exit watch on ``proc`` for :func:`peek_exit`. Call it
    right after the spawn: a process that already exited can no longer be watched (``ESRCH``). Idempotent.

    State on ``proc``: ``_orch_exit_kq`` is the armed kqueue, or ``None`` when it was too late to arm;
    ``_orch_exit_status`` is the wait status once the exit was seen (the event fires only once)."""
    if hasattr(os, "waitid") or hasattr(proc, "_orch_exit_kq"):
        return
    kq = select.kqueue()
    fflags = select.KQ_NOTE_EXIT | NOTE_EXITSTATUS
    event = select.kevent(proc.pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD, fflags=fflags)
    try:
        ack = kq.control([event], 1, 0)
        late = any(ev.flags & select.KQ_EV_ERROR for ev in ack)
    except ProcessLookupError:
        ack, late = [], True
    if late:
        kq.close()
        proc._orch_exit_kq = None  # type: ignore[attr-defined]
        return
    proc._orch_exit_kq = kq  # type: ignore[attr-defined]
    if ack:  # it exited while the watch was armed
        _seen(proc, ack[0].data)


def _seen(proc: subprocess.Popen[bytes], status: int) -> None:
    proc._orch_exit_status = status  # type: ignore[attr-defined]
    proc._orch_exit_kq.close()  # type: ignore[attr-defined]


def _peek_kqueue(proc: subprocess.Popen[bytes]) -> int | None:
    with _EXIT_LOCK:
        return _peek_kqueue_locked(proc)


def _peek_kqueue_locked(proc: subprocess.Popen[bytes]) -> int | None:
    watch_exit(proc)
    if not hasattr(proc, "_orch_exit_status"):
        kq = proc._orch_exit_kq  # type: ignore[attr-defined]
        if kq is None:
            return proc.poll()  # exited before it could be watched: reaping is the only way to its status
        events = kq.control(None, 1, 0)
        if not events:
            return None
        _seen(proc, events[0].data)
    return os.waitstatus_to_exitcode(proc._orch_exit_status)  # type: ignore[attr-defined]


def peek_exit(proc: subprocess.Popen[bytes]) -> int | None:
    """The leader's exit status in ``returncode`` form, **without reaping it** (``waitid(WNOWAIT)``, or on macOS,
    which has no ``waitid``, the kqueue watch of :func:`watch_exit`): until it is reaped its PID, and so the run's
    session and group id, cannot be reused."""
    if proc.returncode is not None:
        return proc.returncode
    if not hasattr(os, "waitid"):
        return _peek_kqueue(proc)
    try:
        res = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return proc.poll()
    if res is None:
        return None
    return res.si_status if res.si_code == os.CLD_EXITED else -res.si_status


def _signame(code: int | None) -> tuple[int | None, str | None]:
    if code is None:
        return None, None
    if code < 0:
        try:
            return None, signal.Signals(-code).name
        except ValueError:
            return None, str(-code)
    return code, None


class Supervisor:
    """Every run of one runner. Thread-safe; :meth:`open` starts the I/O thread, :meth:`close` ends it."""

    def __init__(
        self,
        state_dir: Path,
        *,
        uid: int | None = None,
        cgroups: CgroupTree | None | Literal["detect"] = "detect",
        scan_interval: float = SCAN_SECONDS,
        exit_grace: float = STOP_GRACE,
        ring_bytes: int = RING_BYTES,
        python: str | None = None,
        umask: int | None = None,
        on_event: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.uid = os.getuid() if uid is None else uid
        self.table = RunTable(Path(state_dir) / "runner", self.uid)
        self.cgroups = detect() if cgroups == "detect" else cgroups
        self.boot = procs.boot_id()
        self.scan_interval = scan_interval
        self.exit_grace = exit_grace
        self.ring_bytes = ring_bytes
        self.python = python or sys.executable
        #: the umask runs start with (§12.5: the person's, captured by ``orch runner enable``; else 022)
        self.umask = umask
        self._lock_fd: int | None = None
        self.on_event = on_event
        #: the term socket's wake-up (H11b): called with a run id after new output, the leader's exit and the PTY's
        #: close, from the I/O thread; it must not block
        self.on_output: Callable[[str], None] | None = None
        self.live: dict[str, Live] = {}
        self.lock = threading.RLock()
        self._spawn_lock = threading.RLock()  # re-entrant: the host link checks its epoch and spawns under it
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, None)
        self._thread: threading.Thread | None = None
        self._closing = False
        #: set by :meth:`close`: no spawn after it, and the wake pipe is -1 once released (never touched again)
        self._closed = False
        #: under ``lock``: the I/O thread has ended; close() deferred the release to it; the release happened
        self._io_done = False
        self._release_deferred = False
        self._released = False

    # -- life cycle
    def open(self) -> list[dict[str, Any]]:
        """Load the run table, sweep what a crashed runner left (:meth:`crash_sweep`) and start the I/O thread."""
        self._take_lock()
        if self.umask is None:
            self.umask = read_umask(self.table.directory, self.uid)
        self.table.load()
        swept = self.crash_sweep()
        self._remove_stale_cgroups()
        self._thread = threading.Thread(target=self._loop, name="orch-runner-io", daemon=True)
        self._thread.start()
        return swept

    def _take_lock(self) -> None:
        """One runner per state directory: a second one refuses to start and never sweeps the first one's runs."""
        from orch.host.custody import ensure_private_dir

        ensure_private_dir(self.table.directory, self.uid)
        fd = os.open(self.table.directory / "runner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RunnerBusy(f"another runner holds {self.table.directory}") from None
        self._lock_fd = fd

    def _remove_stale_cgroups(self) -> None:
        if self.cgroups is None:
            return
        for run, rec in self.table.all():
            if rec.get("state") not in ("spawning", "running") and rec.get("cleanup") == "complete":
                cg = self._cgroup_of({**rec, "run": run})
                if cg is not None:
                    self.cgroups.remove(cg)

    def close(self, *, stop_runs: bool = False, grace: float = STOP_GRACE) -> None:
        """Stop the I/O thread and release the PTYs, the selector, the wake pipe and ``runner.lock``, each once.

        Spawns are fenced first: one in progress finishes and is part of the ``stop_runs`` snapshot, later ones are
        refused. When the I/O thread outlives :data:`JOIN_SECONDS`, nothing it may still use is closed under it: the
        thread releases everything itself as it ends, ``runner.lock`` last, so no other runner starts before."""
        with self._spawn_lock:
            self._closed = True
            with self.lock:
                runs = list(self.live) if stop_runs else []
        try:
            for run in runs:
                try:
                    self.stop(run, grace=grace)
                except Exception:  # noqa: BLE001 - one run (pruned, or a failed stop) never keeps close() half done
                    LOG.exception("run %s: stop at close failed", run)
        finally:
            self._closing = True
            self._wake()
            if self._thread is not None:
                self._thread.join(timeout=JOIN_SECONDS)
        with self.lock:
            if self._thread is not None and not self._io_done:
                if not self._release_deferred:
                    self._release_deferred = True
                    LOG.error("runner I/O thread still running after close: it releases its fds when it ends")
                return
        self._release()

    def _release(self) -> None:
        """Close every fd the I/O thread uses, then ``runner.lock``: exactly once, from close() or the ending thread.
        Each attribute is cleared before its number is closed, so nothing touches a reused number afterwards."""
        with self.lock:
            if self._released:
                return
            self._released = True
            for live in self.live.values():
                if not live.pty_closed:
                    live.pty_closed = True
                    try:
                        os.close(live.master)
                    except OSError:
                        pass
            with contextlib.suppress(OSError):
                self._sel.close()
            wake, (self._wake_r, self._wake_w) = (self._wake_r, self._wake_w), (-1, -1)
            lock_fd, self._lock_fd = self._lock_fd, None
        for fd in wake:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                LOG.error("runner.lock could not be released (fd %d)", lock_fd, exc_info=True)

    def _wake(self) -> None:
        with self.lock:  # never a write to a released (and perhaps reused) number
            if self._wake_w < 0:
                return
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def _emit(self, kind: str, run: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, run, payload)
            except Exception:  # noqa: BLE001 - a report must never break supervision
                LOG.exception("runner event hook failed")

    # -- spawn
    def spawn(self, spec: SpawnSpec) -> dict[str, Any]:
        """Start ``spec`` once. A run id that is already recorded returns that record (``duplicate: true``) and starts
        nothing. Raises :class:`SpawnError` (``launch.cwd_changed``, ``harness.not_found``, ``run.spawn_failed``)."""
        spec.check()
        with self._spawn_lock:
            if self._closed:
                raise SpawnError("run.spawn_failed", "the runner is closed", "complete")
            have = self.table.get(spec.run)
            if have is not None:
                return self._summary(spec.run, have) | {"duplicate": True}
            if time.time() - run_time(spec.run) > KEEP_SECONDS:  # its record may be pruned: dedupe could not hold
                raise SpawnError("invalid.input", "spawn: the run id is older than the run table keeps records")
            self.table.put(
                spec.run,
                {
                    "workspace_id": spec.workspace_id,
                    "kind": spec.kind,
                    "state": "spawning",
                    "created_at": now_iso(),
                    "boot": self.boot,
                },  # fmt: skip
            )
            try:
                live = self._start(spec)
            except BaseException as exc:
                err = (
                    exc
                    if isinstance(exc, SpawnError)
                    else SpawnError(
                        "run.spawn_failed", f"the run could not be started: {type(exc).__name__}", "unknown"
                    )
                )
                # a process left by the failed spawn keeps its record (pid, birth): the crash sweep and
                # the clean-up retry (_retry) go on stopping it; the run is never dropped from the table
                self.table.update(
                    spec.run,
                    state="exited",
                    cleanup=err.cleanup,
                    exit={"code": None, "signal": None, "at": now_iso(), "reason": "spawn_failed", "error": err.code},
                )
                if err is exc:
                    raise
                raise err from None
            with self.lock:
                self.live[spec.run] = live
                self._sel.register(live.master, selectors.EVENT_READ, live)
            self._wake()
            return self._summary(spec.run, self.table.get(spec.run) or {}) | {"duplicate": False}

    def _start(self, spec: SpawnSpec) -> Live:
        cg = None
        if self.cgroups is not None:
            try:
                cg = self.cgroups.create(spec.run)
            except OSError as exc:
                raise SpawnError("run.spawn_failed", f"the run cgroup could not be created ({exc.strerror})") from None
        master, slave = os.openpty()
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", spec.rows, spec.cols, 0, 0))
            spec_r, spec_w = os.pipe()
            status_r, status_w = os.pipe()
            payload = {
                "argv": spec.argv,
                "env": spec.env,
                "cwd": {"path": spec.cwd["path"], "dev": spec.cwd["dev"], "ino": spec.cwd["ino"]},
                "cgroup": str(cg) if cg is not None else None,
            }
            try:
                proc = subprocess.Popen(
                    [self.python, "-I", str(EXEC), str(spec_r), str(status_w)],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    pass_fds=(spec_r, status_w),
                    close_fds=True,
                    env={},
                    cwd="/",
                    umask=self.umask,
                )
            except BaseException as exc:  # nothing was forked: nothing ran, nothing is left
                for fd in (spec_w, status_r):
                    with contextlib.suppress(OSError):
                        os.close(fd)
                if cg is not None:
                    self.cgroups.remove(cg)  # type: ignore[union-attr]
                raise SpawnError("run.spawn_failed", f"the trampoline could not be started: {type(exc).__name__}",
                                 "complete") from None  # fmt: skip
            finally:
                os.close(spec_r)
                os.close(status_w)
                os.close(slave)
                slave = -1
            pipes = {"spec": spec_w, "status": status_r}  # still ours to close
            try:
                # recorded before anything else can fail: no forked process is ever without a run-table entry
                first = procs.info(proc.pid)
                self.table.update(
                    spec.run, pid=proc.pid, birth=first.birth if first else None, pgid=proc.pid, sid=proc.pid,
                    cgroup=str(cg) if cg is not None else None,
                )  # fmt: skip
                with _EXIT_LOCK:
                    watch_exit(proc)  # armed before the spec is written: until then the trampoline only waits for it
                try:
                    try:
                        handle = os.fdopen(pipes.pop("spec"), "wb")  # the file object owns the fd from here
                        with handle:
                            handle.write(json.dumps(payload).encode())
                    except BrokenPipeError:
                        pass
                    status = self._read_status(status_r)
                finally:
                    os.close(pipes.pop("status"))
                if status is not None:
                    raise _spawn_error(status)
                found = procs.info(proc.pid)
                if found is None:
                    raise SpawnError("run.spawn_failed", "the run ended before it was recorded")
                self.table.update(
                    spec.run, state="running", pid=proc.pid, birth=found.birth, pgid=found.pgid, sid=found.sid,
                    cgroup=str(cg) if cg is not None else None, started_at=now_iso(), escaped=False, escapees=[],
                )  # fmt: skip
            except BaseException as exc:
                for fd in pipes.values():  # never an fd the file object already closed (it could be reused)
                    with contextlib.suppress(OSError):
                        os.close(fd)
                cleanup = self._abort(spec.run, proc, cg)
                if isinstance(exc, SpawnError):
                    exc.cleanup = cleanup
                    raise
                raise SpawnError(
                    "run.spawn_failed", f"the run could not be started: {type(exc).__name__}", cleanup
                ) from None
            os.set_blocking(master, False)
            live = Live(spec.run, proc, master, Ring(self.ring_bytes), proc.pid, found.birth, found.pgid, found.sid, cg)
            live.cols, live.rows = spec.cols, spec.rows
            return live
        except BaseException:
            if slave != -1:
                os.close(slave)
            os.close(master)
            raise

    def _abort(self, run: str, proc: subprocess.Popen[bytes], cg: Path | None) -> str:
        """A spawn failed after the fork: wait briefly for the trampoline's own exit (a failed step exits 127), kill
        it if it hangs, sweep its session, and say honestly what is left."""
        deadline = time.monotonic() + KILL_WAIT
        while peek_exit(proc) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if peek_exit(proc) is None:
            proc.kill()  # our unreaped child: its PID cannot have been reused
            deadline = time.monotonic() + KILL_WAIT
            while peek_exit(proc) is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if peek_exit(proc) is None:
                return "incomplete"
        rec = {**(self.table.get(run) or {}), "run": run}
        cleanup: str = "complete"
        if self._members(rec, {}, live=True):  # the leader is not reaped yet: its session is still the run's
            cleanup = self._terminate(rec, {}, 0.5, live=True)
        proc.wait()
        if cg is not None and cleanup == "complete":
            self.cgroups.remove(cg)  # type: ignore[union-attr]
        return cleanup

    def _read_status(self, fd: int) -> dict[str, Any] | None:
        """``None`` once the trampoline exec'd (EOF without bytes); otherwise its failure record."""
        deadline = time.monotonic() + SPAWN_TIMEOUT
        data = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return {"step": "timeout", "errno": 0}
            if not fdwait.readable(fd, left):
                continue
            part = os.read(fd, 4096)
            if not part:
                break
            data += part
        if not data:
            return None
        try:
            doc = json.loads(data)
            return doc if isinstance(doc, dict) else {"step": "internal", "errno": 0}
        except ValueError:
            return {"step": "internal", "errno": 0}

    def _summary(self, run: str, rec: dict[str, Any]) -> dict[str, Any]:
        keys = ("pid", "pgid", "sid", "birth", "state", "cleanup", "exit", "started_at")
        return {"run": run, **{k: rec.get(k) for k in keys}}

    # -- reads
    def get(self, run: str) -> dict[str, Any] | None:
        rec = self.table.get(run)
        if rec is None:
            return None
        out = self._summary(run, rec) | {"workspace_id": rec.get("workspace_id"), "kind": rec.get("kind")}
        live = self.live.get(run)
        out["out"] = live.ring.end if live is not None else None
        return out

    def runs(self) -> list[dict[str, Any]]:
        return [r for r in (self.get(k) for k, _ in self.table.all()) if r is not None]

    def ring(self, run: str) -> Ring | None:
        live = self.live.get(run)
        return None if live is None else live.ring

    def resize(self, run: str, cols: int, rows: int) -> None:
        if not (20 <= cols <= 500 and 5 <= rows <= 200):
            raise SpawnError("invalid.input", "size out of range")
        with self.lock:  # the master is never closed (and its fd reused) under a resize
            live = self.live.get(run)
            if live is None or live.pty_closed:
                raise SpawnError("not_found", "no such live run")
            fcntl.ioctl(live.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            live.cols, live.rows = cols, rows

    def live_of(self, run: str) -> Live | None:
        """The run's supervision record while its ring is kept (the term socket reads ``ring``, ``exit``,
        ``pty_closed``, ``cols`` and ``rows`` from it), else ``None``."""
        return self.live.get(run)

    def write_input(self, run: str, data: bytes) -> int:
        """One non-blocking write of terminal input to the run's PTY master (H11b): the bytes written, ``0`` when the
        PTY's input queue is full. Refused once the leader exited or the master is closed (the input never arrived)."""
        with self.lock:  # the master is never closed (and its fd reused) under a write
            live = self.live.get(run)
            if live is None or live.pty_closed or live.exit is not None:
                raise SpawnError("not_found", "no such live run")
            try:
                return os.write(live.master, data)
            except BlockingIOError:
                return 0
            except OSError as exc:
                raise SpawnError("not_found", f"the terminal is gone ({exc.errno})") from None

    def nudge(self, run: str) -> None:
        """Make full-screen programs redraw (a new writer took the lease, §12.6.2): two size changes, so the kernel
        sends the PTY's foreground group ``SIGWINCH``; the size ends where it was. No signal is sent by the runner."""
        with self.lock:
            live = self.live.get(run)
            if live is None or live.pty_closed or live.exit is not None:
                return
            other = live.rows + 1 if live.rows < 200 else live.rows - 1
            with contextlib.suppress(OSError):
                fcntl.ioctl(live.master, termios.TIOCSWINSZ, struct.pack("HHHH", other, live.cols, 0, 0))
                fcntl.ioctl(live.master, termios.TIOCSWINSZ, struct.pack("HHHH", live.rows, live.cols, 0, 0))

    def _out(self, run: str) -> None:
        hook = self.on_output
        if hook is not None:
            try:
                hook(run)
            except Exception:  # noqa: BLE001 - a viewer never disturbs supervision
                LOG.exception("output hook failed")

    def attach_info(self, run: str) -> dict[str, Any] | None:
        """What an attachment's ``ready`` frame needs (§12.6.3), or ``None`` when the run's ring is not kept."""
        live = self.live.get(run)
        if live is None:
            return None
        return {"start": live.ring.start, "offset": live.ring.end, "cols": live.cols, "rows": live.rows,
                "state": "running" if live.exit is None else "exited"}  # fmt: skip

    # -- membership
    def _cgroup_of(self, rec: dict[str, Any]) -> Path | None:
        """The run's cgroup, only if it sits directly under the delegated base and is named ``run-<run id>`` (an
        edited table can never point ``cgroup.kill`` elsewhere)."""
        cg, run = rec.get("cgroup"), rec.get("run")
        if not cg or self.cgroups is None or not isinstance(run, str):
            return None
        try:
            want = self.cgroups.path(run)
        except ValueError:
            return None
        return want if Path(cg) == want else None

    def _members(
        self, rec: dict[str, Any], tracked: dict[int, int], snap: dict[int, procs.ProcInfo] | None = None,
        *, live: bool = False,
    ) -> dict[int, tuple[int, int]]:  # fmt: skip
        """``{pid: (birth, uid)}`` of the run's processes now, of **every** UID (only our own are ever signalled;
        another UID's member makes the cleanup incomplete). Zombies, PID ≤ 1 and this runner are left out.

        * Another boot than the record's: nothing (birth times do not compare across boots).
        * A cgroup run: the processes in its cgroup, plus tracked escapees by exact identity.
        * Otherwise, exact identities: the leader, the escapees and the members saved at the last scan; and only for
          a ``live`` run of this runner's lifetime, also the session and group born after the leader (skipped when the
          session's PID now names another process). An ended or crash-swept record never matches a session or a
          group: a leaderless session whose id equals an old one belongs to someone else (a double-forked daemon)."""
        out: dict[int, tuple[int, int]] = {}
        pid, birth, pgid, sid = rec.get("pid"), rec.get("birth"), rec.get("pgid"), rec.get("sid")
        if not isinstance(pid, int) or not isinstance(birth, int):
            return out
        if rec.get("boot", self.boot) != self.boot:
            return out
        exact = dict(tracked)
        exact[pid] = birth
        for pair in rec.get("members") or []:
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, int) for x in pair):
                exact.setdefault(pair[0], pair[1])
        cgroup = self._cgroup_of(rec)
        if cgroup is not None:
            for p in self.cgroups.procs(cgroup):  # type: ignore[union-attr]
                found = procs.info(p)
                if found is not None and not found.zombie:
                    out[p] = (found.birth, found.uid)
            snap_e = {p: procs.info(p) for p in exact}
            for p, b in exact.items():
                found = snap_e.get(p)
                if found is not None and found.birth == b and not found.zombie:
                    out[p] = (b, found.uid)
        else:
            snap = procs.snapshot(None) if snap is None else snap
            for p, b in exact.items():
                found = snap.get(p)
                if found is not None and found.birth == b and not found.zombie:
                    out[p] = (b, found.uid)
            if live:
                leader = snap.get(sid) if isinstance(sid, int) else None
                reused = leader is not None and leader.birth != birth  # the session's PID now names another process
                if not reused:
                    for p in snap.values():
                        if not p.zombie and (p.sid == sid or p.pgid == pgid) and p.birth >= birth:
                            out[p.pid] = (p.birth, p.uid)
        out.pop(os.getpid(), None)
        for p in [p for p in out if p <= 1]:
            del out[p]
        return out

    def scan(self) -> None:
        """The escape scan: follow parent links (every UID) from every live run's processes, track descendants
        outside the run's session (or, with a cgroup, outside its cgroup) by birth identity, and save the run's
        member set, so a later sweep needs exact identities only."""
        with self.lock:
            runs = [lv for lv in self.live.values() if lv.cleanup is None]
        if not runs:
            return
        try:
            snap = procs.snapshot(None)
        except OSError:
            return
        children: dict[int, list[procs.ProcInfo]] = {}
        for p in snap.values():
            children.setdefault(p.ppid, []).append(p)
        for live in runs:
            self._scan_run(live, snap, children)

    def _scan_run(self, live: Live, snap: dict[int, procs.ProcInfo], children: dict[int, list[procs.ProcInfo]]):
        rec = self._rec(live)
        known = {p: bu[0] for p, bu in self._members(rec, live.tracked, snap, live=live.session_ok).items()}
        inside = set(known)
        queue = list(known)
        while queue:
            parent = queue.pop()
            for child in children.get(parent, ()):
                if child.pid not in known and not child.zombie and child.birth >= live.birth:
                    known[child.pid] = child.birth
                    queue.append(child.pid)
        if live.cgroup is not None:
            outside = {p: b for p, b in known.items() if p not in inside}
        else:
            outside = {
                p: b for p, b in known.items()
                if snap.get(p) is not None and snap[p].sid not in (live.sid, -1)  # -1: exited while read
            }  # fmt: skip
        new = {p: b for p, b in outside.items() if live.tracked.get(p) != b}
        fields: dict[str, Any] = {}
        if new:
            live.tracked.update(new)
            if not live.escaped:
                LOG.warning("run %s: %d process(es) left the run's session or cgroup", live.run, len(new))
            live.escaped = True
            fields.update(escaped=True, escapees=[[p, b] for p, b in live.tracked.items()][:256])
        members = sorted([p, b] for p, b in known.items())[:256]
        if members != live.saved:
            live.saved = members
            fields["members"] = members
        if fields:
            self.table.update(live.run, **fields)

    # -- stop
    def stop(self, run: str, *, grace: float = STOP_GRACE) -> dict[str, Any]:
        """Stop a run: ``SIGHUP``+``SIGTERM``, ``grace`` seconds, ``SIGKILL``, then the check. Returns ``{state,
        cleanup}``; a run that already ended returns its recorded result."""
        with self.lock:
            live = self.live.get(run)
        if live is None:
            rec = self.table.get(run)
            if rec is None:
                raise SpawnError("not_found", "no such run")
            return {"state": rec.get("state"), "cleanup": rec.get("cleanup") or "unknown"}
        live.stopping = True
        if live.cleanup == "incomplete":
            self._retry(live)
        else:
            self._finish(live, grace)
        rec = self.table.get(run) or {}
        return {"state": rec.get("state"), "cleanup": live.cleanup or "unknown"}

    def _retry(self, live: Live) -> None:
        """A run whose cleanup was incomplete: ``SIGKILL`` to what is still there (by birth identity), reap the
        leader if it finally died. ``complete`` only when nothing is left and nothing ever escaped."""
        with live.stop_lock:
            if live.cleanup != "incomplete":
                return
            rec = self._rec(live)
            members = self._members(rec, live.tracked, live=live.session_ok)
            if self._cgroup_of(rec) is not None:
                self.cgroups.kill(live.cgroup)  # type: ignore[union-attr,arg-type]
            for p, (b, u) in members.items():
                if u == self.uid:
                    procs.signal_verified(p, b, signal.SIGKILL)
            time.sleep(0.05)
            if peek_exit(live.proc) is not None and live.exit is None:
                self._leader_exit(live)
            if peek_exit(live.proc) is not None and not live.reaped:
                live.proc.wait()
                live.reaped = True
            left = self._members(rec, live.tracked, live=live.session_ok)
            live.lingering = bool(left) or not live.reaped
            if not live.lingering and live.escaped and not live.done.is_set():
                live.ended_at = time.monotonic()  # nothing seen is left: prunable, but it stays "incomplete"
                live.done.set()
            if not live.lingering and not live.escaped and live.exit is not None:
                live.cleanup = "complete"
                if self._cgroup_of(rec) is not None:
                    self.cgroups.remove(live.cgroup)  # type: ignore[union-attr,arg-type]
                self.table.update(live.run, cleanup="complete")
                live.ended_at = time.monotonic()
                live.done.set()
                self._emit("cleanup", live.run, {"cleanup": "complete"})

    def _rec(self, live: Live) -> dict[str, Any]:
        return {"run": live.run, "pid": live.pid, "birth": live.birth, "pgid": live.pgid, "sid": live.sid,
                "cgroup": str(live.cgroup) if live.cgroup else None, "boot": self.boot,
                "members": live.saved}  # fmt: skip

    def _finish(self, live: Live, grace: float) -> None:
        with live.stop_lock:
            if live.cleanup is not None:
                return
            try:
                self.scan()
                cleanup = self._terminate(
                    self._rec(live),
                    live.tracked,
                    grace,
                    escaped=lambda: live.escaped,
                    leader_done=lambda: peek_exit(live.proc) is not None,
                    live=live.session_ok,
                )
            except Exception:  # noqa: BLE001 - the clean-up never dies silently: unknown, and retried
                LOG.exception("run %s: clean-up failed", live.run)
                cleanup = "incomplete"
            deadline = time.monotonic() + KILL_WAIT
            while peek_exit(live.proc) is None and time.monotonic() < deadline:
                time.sleep(0.02)
            reaped = peek_exit(live.proc) is not None
            if reaped:
                self._leader_exit(live)  # the run ended; a leader still alive is never reported as exited
                if not live.reaped:
                    live.proc.wait()  # reaped only now, after the session sweep: from here on, exact identities only
                    live.reaped = True
            else:
                cleanup = "incomplete"
            if live.cgroup is not None and cleanup == "complete":
                self.cgroups.remove(live.cgroup)  # type: ignore[union-attr]
            live.cleanup = cleanup
            live.lingering = cleanup == "incomplete" and (
                not reaped
                or bool(self._members(self._rec(live), live.tracked, live=live.session_ok))
                or cleanup == "incomplete"
            )
            self.table.update(live.run, cleanup=cleanup)
            if cleanup != "incomplete" or not live.lingering:
                live.ended_at = time.monotonic()
                live.done.set()
        self._emit("cleanup", live.run, {"cleanup": cleanup})

    def _terminate(
        self,
        rec: dict[str, Any],
        tracked: dict[int, int],
        grace: float,
        *,
        escaped: Callable[[], bool] = lambda: False,
        leader_done: Callable[[], bool] = lambda: True,
        live: bool = False,
    ) -> Cleanup:
        """The stop sequence against the run's members; signals go only to processes of this runner's UID. Any member
        left at the end, of whatever UID, any escape, or a cgroup still populated makes it ``incomplete``.
        ``complete`` with a cgroup means "nothing is left in the run's cgroup and nothing was seen leaving it": a
        same-UID run can move itself out of a writable delegated cgroup between two scans."""
        cgroup = self._cgroup_of(rec)

        def members() -> dict[int, tuple[int, int]]:
            return self._members(rec, tracked, live=live)

        def own(m: dict[int, tuple[int, int]]) -> dict[int, int]:
            return {p: b for p, (b, u) in m.items() if u == self.uid}

        current = members()
        for p, b in own(current).items():
            procs.signal_verified(p, b, signal.SIGHUP)
            procs.signal_verified(p, b, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and (own(current) or not leader_done()):
            time.sleep(0.05)
            current = members()
        if own(current) or (cgroup is not None and self.cgroups.populated(cgroup)):  # type: ignore[union-attr]
            if cgroup is not None:
                self.cgroups.kill(cgroup)  # type: ignore[union-attr]
            for p, b in own(current).items():
                procs.signal_verified(p, b, signal.SIGKILL)
            deadline = time.monotonic() + KILL_WAIT
            while time.monotonic() < deadline:
                current = members()
                if not own(current) and (cgroup is None or self.cgroups.populated(cgroup) is False):  # type: ignore[union-attr]
                    break
                time.sleep(0.05)
        left = members()
        if cgroup is not None:
            state = self.cgroups.populated(cgroup)  # type: ignore[union-attr]
            if state is None:
                return "unknown"
            return "complete" if state is False and not left and not escaped() else "incomplete"
        if left or escaped():
            return "incomplete"
        return "complete"

    # -- crash sweep
    def crash_sweep(self, grace: float = 2.0) -> list[dict[str, Any]]:
        """After a runner crash: every run still recorded live becomes ``lost`` with ``cleanup: "unknown"``; the
        recorded processes that still match their birth identity get the stop sequence. Returns what was found."""
        found = []
        for run, rec in self.table.all():
            if rec.get("state") not in ("spawning", "running"):
                if rec.get("cleanup") not in (None, "incomplete") or not isinstance(rec.get("pid"), int):
                    continue
                pairs = rec.get("escapees") or []
                tracked = {p: b for p, b in pairs if isinstance(p, int) and isinstance(b, int)}
                rec = {**rec, "run": run}
                members = self._members(rec, tracked)  # exact identities only (never a session match)
                if members:  # left over by an incomplete cleanup or a failed spawn: stop it, still not "complete"
                    self._terminate(rec, tracked, grace)
                    self.table.update(run, cleanup="unknown", swept=len(members))
                    found.append({"run": run, "workspace_id": rec.get("workspace_id"), "swept": len(members)})
                continue
            pairs = rec.get("escapees") or []
            tracked = {p: b for p, b in pairs if isinstance(p, int) and isinstance(b, int)}
            rec = {**rec, "run": run}
            members = self._members(rec, tracked)  # exact identities only (never a session match)
            if members:
                self._terminate(rec, tracked, grace)
            self.table.update(
                run,
                state="lost",
                cleanup="unknown",
                exit={"code": None, "signal": None, "at": now_iso(), "reason": "lost"},
                swept=len(members),
            )
            found.append({"run": run, "workspace_id": rec.get("workspace_id"), "swept": len(members)})
        return found

    # -- the I/O thread
    def _loop(self) -> None:
        """The I/O thread. Every step is guarded: an unexpected error is logged, the run it concerns is recorded with
        ``cleanup: "unknown"`` (when it has none yet), and supervision goes on. The thread never dies silently."""
        next_scan = time.monotonic() + self.scan_interval
        try:
            while not self._closing:
                try:
                    next_scan = self._step(next_scan)
                except Exception:  # noqa: BLE001 - never a dead I/O thread
                    LOG.exception("runner I/O step failed")
                    time.sleep(0.1)
        finally:
            with self.lock:  # the same lock close() decides under: exactly one of the two releases
                self._io_done = True
                deferred = self._release_deferred
            if deferred:
                self._release()

    def _guard(self, live: Live, what: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            LOG.exception("run %s: %s failed", live.run, what)
            with contextlib.suppress(Exception):
                if (self.table.get(live.run) or {}).get("cleanup") is None:
                    self.table.update(live.run, cleanup="unknown")

    def _step(self, next_scan: float) -> float:
        try:
            events = self._sel.select(timeout=0.1)
        except (OSError, ValueError):
            events = []
        for key, _ in events:
            if key.data is None:
                fd = self._wake_r
                if fd < 0 or self._closing:  # released, or about to be: never read a reused number
                    continue
                try:
                    while os.read(fd, 4096):
                        pass
                except BlockingIOError:
                    pass
                continue
            self._guard(key.data, "read", lambda lv=key.data: self._read(lv))
        now = time.monotonic()
        with self.lock:
            lives = list(self.live.values())
        for live in lives:
            self._guard(live, "leader exit", lambda lv=live, n=now: self._watch(lv, n))
        self._prune(now)
        if now >= next_scan:
            next_scan = now + self.scan_interval
            for live in lives:
                if live.lingering and live.cleanup == "incomplete":
                    self._guard(live, "clean-up retry", lambda lv=live: self._retry(lv))
            try:
                self.scan()
            except Exception:  # noqa: BLE001 - the scan is best effort; it never stops the I/O
                LOG.exception("escape scan failed")
        return next_scan

    def _watch(self, live: Live, now: float) -> None:
        if live.exit is None and peek_exit(live.proc) is not None:  # seen, not reaped: _finish reaps after the sweep
            self._leader_exit(live)
            if not live.stopping:
                threading.Thread(target=self._finish, args=(live, self.exit_grace), daemon=True).start()
        if live.exit is not None and not live.pty_closed and now >= live.drain_until:
            self._close_pty(live)

    def _prune(self, now: float) -> None:
        with self.lock:
            ended = sorted(
                (lv.ended_at, run)
                for run, lv in self.live.items()
                if lv.done.is_set() and lv.pty_closed and not lv.lingering
            )
            drop = [run for at, run in ended if now - at > KEEP_RING_SECONDS]
            drop += [run for _, run in ended[: max(0, len(ended) - KEEP_RINGS)] if run not in drop]
            for run in drop:
                del self.live[run]

    def _read(self, live: Live) -> None:
        for _ in range(16):  # at most 1 MiB per wake-up for one run, so no run starves the others
            try:
                data = os.read(live.master, 65536)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    data = b""
                else:
                    raise
            if not data:
                self._close_pty(live)
                return
            live.ring.write(data)
            self._out(live.run)

    def _close_pty(self, live: Live) -> None:
        with self.lock:
            if live.pty_closed:
                return
            live.pty_closed = True
            if live.exit is not None:  # the drained total, after the leader ended
                with contextlib.suppress(Exception):
                    self.table.update(live.run, out=live.ring.end)
            try:
                self._sel.unregister(live.master)
            except (KeyError, ValueError):
                pass
            try:
                os.close(live.master)
            except OSError:
                pass
        self._out(live.run)

    def _leader_exit(self, live: Live) -> None:
        with self.lock:
            if live.exit is not None:
                return
            code, sig = _signame(peek_exit(live.proc))
            live.exit = {
                "code": code,
                "signal": sig,
                "at": now_iso(),
                "reason": "stopped" if live.stopping else "exited",
            }
            live.drain_until = time.monotonic() + DRAIN_SECONDS
        self.table.update(live.run, state="exited", exit=live.exit, out=live.ring.end)
        self._wake()
        self._emit("exit", live.run, dict(live.exit))
        self._out(live.run)


def _spawn_error(status: dict[str, Any]) -> SpawnError:
    step, code = str(status.get("step")), status.get("errno") or 0
    if step == "cwd":
        return SpawnError("launch.cwd_changed", "the pinned working directory no longer matches")
    if step == "exec" and code in (errno.ENOENT, errno.ENOTDIR):
        return SpawnError("harness.not_found", "the program does not exist")
    reason = os.strerror(code) if isinstance(code, int) and code else "failed"
    return SpawnError("run.spawn_failed", f"the trampoline failed at step {step}: {reason}")
