"""Run supervision (D-P2-29, D-P2-37, D-P2-39): spawn through the trampoline, PTY and ring, stop, sweeps, cleanup.

**A run is its session.** The trampoline (:mod:`orch.runner.exec`) makes the run's first process a session leader
with the PTY as controlling terminal; its PID is the run's pid, process group and session id. The runner keeps the
PTY master, reads it continuously into a 1 MiB :class:`~orch.runner.ring.Ring` (a slow or absent viewer never blocks a
run) and reaps the leader.

**Stop** (``stop`` and the clean-up after a leader exits): ``SIGHUP`` and ``SIGTERM`` to every member, a grace period,
``SIGKILL`` to what is left, then a check. Members are found per platform:

* Linux with a delegated cgroup (:mod:`orch.runner.cgroup`): the run's cgroup, joined by the trampoline before exec;
  the kill is ``cgroup.kill`` and ``cleanup`` is ``complete`` exactly when ``cgroup.events`` says ``populated 0``.
* macOS, and Linux without delegation: the processes of the runner's UID in the run's process group or session that
  were born after the leader, plus every descendant the escape scan saw. This is **best-effort cleanup, not
  containment**: a descendant that calls ``setsid()`` leaves the session. The scan (every ``scan_interval`` seconds and
  right before a stop) follows parent links from the run's processes; a descendant found outside the session marks the
  run ``escaped`` and is tracked by birth identity. A run that ever escaped reports ``cleanup: "incomplete"`` even when
  every tracked process is gone, because a descendant that escaped between two scans cannot be seen.

**Never a reused PID.** Every signal goes through :func:`orch.runner.procs.signal_verified` with the recorded birth
time; the session and group sweep is skipped when the session leader's PID now belongs to a process with another
birth time (the PID was reused, so that session is not the run's). Never by name, never UID-wide, never a group
signal.

**Persisted first.** The run record is written (state ``spawning``) before the process exists, and a ``spawn`` whose
run id is already recorded never starts a second process: a lost ack, a host reconnect or a host restart between
intent and ack gives exactly one process. After a runner crash, :meth:`Supervisor.open` marks every run still live as
``lost`` with ``cleanup: "unknown"`` and sweeps the recorded processes that still match their birth identity.

**Run end** is the leader's exit; the PTY is then drained for at most 2 s and the master closed (never waiting for EOF,
which a background process holding the slave would delay forever); the remaining members get the stop sequence.
"""

from __future__ import annotations

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

from . import procs
from .cgroup import CgroupTree, detect
from .ring import RING_BYTES, Ring
from .table import RUN_ID, RunTable, now_iso

__all__ = ["EXEC", "SpawnError", "SpawnSpec", "Supervisor"]

LOG = logging.getLogger(__name__)

#: the trampoline, run as ``python -I <EXEC> SPEC_FD STATUS_FD`` (``-I`` implies ``-P``: no script dir on the path)
EXEC = Path(__file__).with_name("exec.py")
SPAWN_TIMEOUT = 10.0
DRAIN_SECONDS = 2.0
KILL_WAIT = 2.0
STOP_GRACE = 10.0
SCAN_SECONDS = 2.0
#: an ended run's ring is kept 1 h, for at most 64 ended runs (§9.1)
KEEP_RING_SECONDS = 3600.0
KEEP_RINGS = 64
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
Cleanup = Literal["complete", "incomplete", "unknown"]


class SpawnError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


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
    done: threading.Event = field(default_factory=threading.Event)
    ended_at: float = 0.0
    stop_lock: threading.Lock = field(default_factory=threading.Lock)


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
        on_event: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.uid = os.getuid() if uid is None else uid
        self.table = RunTable(Path(state_dir) / "runner", self.uid)
        self.cgroups = detect() if cgroups == "detect" else cgroups
        self.scan_interval = scan_interval
        self.exit_grace = exit_grace
        self.ring_bytes = ring_bytes
        self.python = python or sys.executable
        self.on_event = on_event
        self.live: dict[str, Live] = {}
        self.lock = threading.RLock()
        self._spawn_lock = threading.Lock()
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, None)
        self._thread: threading.Thread | None = None
        self._closing = False

    # -- life cycle
    def open(self) -> list[dict[str, Any]]:
        """Load the run table, sweep what a crashed runner left (:meth:`crash_sweep`) and start the I/O thread."""
        self.table.load()
        swept = self.crash_sweep()
        self._thread = threading.Thread(target=self._loop, name="orch-runner-io", daemon=True)
        self._thread.start()
        return swept

    def close(self, *, stop_runs: bool = False, grace: float = STOP_GRACE) -> None:
        if stop_runs:
            for run in list(self.live):
                self.stop(run, grace=grace)
        self._closing = True
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self.lock:
            for live in self.live.values():
                if not live.pty_closed:
                    live.pty_closed = True
                    try:
                        os.close(live.master)
                    except OSError:
                        pass
        self._sel.close()
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def _wake(self) -> None:
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
            have = self.table.get(spec.run)
            if have is not None:
                return self._summary(spec.run, have) | {"duplicate": True}
            self.table.put(
                spec.run,
                {"workspace_id": spec.workspace_id, "kind": spec.kind, "state": "spawning", "created_at": now_iso()},
            )
            try:
                live = self._start(spec)
            except SpawnError as exc:
                self.table.update(
                    spec.run,
                    state="exited",
                    cleanup="complete",
                    exit={"code": None, "signal": None, "at": now_iso(), "reason": "spawn_failed", "error": exc.code},
                )
                raise
            except BaseException as exc:
                self.table.update(
                    spec.run,
                    state="exited",
                    cleanup="unknown",
                    exit={"code": None, "signal": None, "at": now_iso(), "reason": "spawn_failed"},
                )
                raise SpawnError("run.spawn_failed", f"the run could not be started: {type(exc).__name__}") from None
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
                )
            finally:
                os.close(spec_r)
                os.close(status_w)
                os.close(slave)
                slave = -1
            try:
                try:
                    with os.fdopen(spec_w, "wb") as f:
                        f.write(json.dumps(payload).encode())
                except BrokenPipeError:
                    pass
                status = self._read_status(status_r)
            finally:
                os.close(status_r)
            if status is not None:
                proc.wait()
                if cg is not None:
                    self.cgroups.remove(cg)  # type: ignore[union-attr]
                raise _spawn_error(status)
            found = procs.info(proc.pid)
            if found is None:
                proc.wait()
                raise SpawnError("run.spawn_failed", "the run ended before it was recorded")
            os.set_blocking(master, False)
            live = Live(spec.run, proc, master, Ring(self.ring_bytes), proc.pid, found.birth, found.pgid, found.sid, cg)
            self.table.update(
                spec.run,
                state="running",
                pid=live.pid,
                birth=live.birth,
                pgid=live.pgid,
                sid=live.sid,
                cgroup=str(cg) if cg is not None else None,
                started_at=now_iso(),
                escaped=False,
                escapees=[],
            )
            return live
        except BaseException:
            if slave != -1:
                os.close(slave)
            os.close(master)
            raise

    def _read_status(self, fd: int) -> dict[str, Any] | None:
        """``None`` once the trampoline exec'd (EOF without bytes); otherwise its failure record."""
        deadline = time.monotonic() + SPAWN_TIMEOUT
        data = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return {"step": "timeout", "errno": 0}
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
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
        live = self.live.get(run)
        if live is None or live.pty_closed:
            raise SpawnError("not_found", "no such live run")
        if not (20 <= cols <= 500 and 5 <= rows <= 200):
            raise SpawnError("invalid.input", "size out of range")
        fcntl.ioctl(live.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    # -- membership
    def _members(self, rec: dict[str, Any], tracked: dict[int, int], snap: dict[int, procs.ProcInfo] | None = None):
        """``{pid: birth}`` of the run's live processes (zombies excluded, never PID ≤ 1, never this runner)."""
        out: dict[int, int] = {}
        pid, birth, pgid, sid = rec.get("pid"), rec.get("birth"), rec.get("pgid"), rec.get("sid")
        cgroup = rec.get("cgroup")
        if not isinstance(pid, int) or not isinstance(birth, int):
            return out
        if cgroup and self.cgroups is not None:
            for p in self.cgroups.procs(Path(cgroup)):
                found = procs.info(p)
                if found is not None and found.uid == self.uid and not found.zombie:
                    out[p] = found.birth
        else:
            snap = procs.snapshot(self.uid) if snap is None else snap
            leader = snap.get(sid) if isinstance(sid, int) else None
            reused = leader is not None and leader.birth != birth  # the session's PID now names another process
            for p in snap.values():
                if p.zombie:
                    continue
                if p.pid == pid and p.birth == birth:
                    out[p.pid] = p.birth
                elif not reused and (p.sid == sid or p.pgid == pgid) and p.birth >= birth:
                    out[p.pid] = p.birth
            for p, b in tracked.items():
                found = snap.get(p)
                if found is not None and found.birth == b and not found.zombie:
                    out[p] = b
        out.pop(os.getpid(), None)
        for p in [p for p in out if p <= 1]:
            del out[p]
        return out

    def scan(self) -> None:
        """The escape scan (macOS and Linux without a cgroup): follow parent links from every live run's processes and
        track descendants outside the run's session by birth identity."""
        with self.lock:
            runs = [lv for lv in self.live.values() if lv.cgroup is None and lv.cleanup is None]
        if not runs:
            return
        try:
            snap = procs.snapshot(self.uid)
        except OSError:
            return
        children: dict[int, list[procs.ProcInfo]] = {}
        for p in snap.values():
            children.setdefault(p.ppid, []).append(p)
        for live in runs:
            self._scan_run(live, snap, children)

    def _scan_run(self, live: Live, snap: dict[int, procs.ProcInfo], children: dict[int, list[procs.ProcInfo]]):
        rec = {"pid": live.pid, "birth": live.birth, "pgid": live.pgid, "sid": live.sid}
        known = self._members(rec, live.tracked, snap)
        queue = list(known)
        while queue:
            parent = queue.pop()
            for child in children.get(parent, ()):
                if child.pid not in known and not child.zombie and child.birth >= live.birth:
                    known[child.pid] = child.birth
                    queue.append(child.pid)
        outside = {p: b for p, b in known.items() if snap.get(p) is not None and snap[p].sid != live.sid}
        new = {p: b for p, b in outside.items() if live.tracked.get(p) != b}
        if new:
            live.tracked.update(new)
            if not live.escaped:
                LOG.warning("run %s: %d process(es) left the run's session", live.run, len(new))
            live.escaped = True
            self.table.update(live.run, escaped=True, escapees=[[p, b] for p, b in live.tracked.items()][:256])

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
        self._finish(live, grace)
        rec = self.table.get(run) or {}
        return {"state": rec.get("state"), "cleanup": live.cleanup or "unknown"}

    def _finish(self, live: Live, grace: float) -> None:
        with live.stop_lock:
            if live.cleanup is not None:
                return
            if live.cgroup is None:
                self.scan()
            cleanup = self._terminate(
                {
                    "pid": live.pid,
                    "birth": live.birth,
                    "pgid": live.pgid,
                    "sid": live.sid,
                    "cgroup": str(live.cgroup) if live.cgroup else None,
                },  # fmt: skip
                live.tracked,
                grace,
                escaped=lambda: live.escaped,
                leader_done=lambda: live.proc.poll() is not None,
            )
            try:
                live.proc.wait(timeout=KILL_WAIT)
            except subprocess.TimeoutExpired:
                cleanup = "incomplete"
            self._leader_exit(live)
            if live.cgroup is not None and cleanup == "complete":
                self.cgroups.remove(live.cgroup)  # type: ignore[union-attr]
            live.cleanup = cleanup
            self.table.update(live.run, cleanup=cleanup)
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
    ) -> Cleanup:
        cgroup = Path(rec["cgroup"]) if rec.get("cgroup") and self.cgroups is not None else None
        members = self._members(rec, tracked)
        for p, b in members.items():
            procs.signal_verified(p, b, signal.SIGHUP)
            procs.signal_verified(p, b, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and (members or not leader_done()):
            time.sleep(0.05)
            members = self._members(rec, tracked)
        if members or (cgroup is not None and self.cgroups.populated(cgroup)):  # type: ignore[union-attr]
            if cgroup is not None:
                self.cgroups.kill(cgroup)  # type: ignore[union-attr]
            for p, b in members.items():
                procs.signal_verified(p, b, signal.SIGKILL)
            deadline = time.monotonic() + KILL_WAIT
            while time.monotonic() < deadline:
                members = self._members(rec, tracked)
                if not members and (cgroup is None or self.cgroups.populated(cgroup) is False):  # type: ignore[union-attr]
                    break
                time.sleep(0.05)
        if cgroup is not None:
            state = self.cgroups.populated(cgroup)  # type: ignore[union-attr]
            return "complete" if state is False else "unknown" if state is None else "incomplete"
        if self._members(rec, tracked) or escaped():
            return "incomplete"
        return "complete"

    # -- crash sweep
    def crash_sweep(self, grace: float = 2.0) -> list[dict[str, Any]]:
        """After a runner crash: every run still recorded live becomes ``lost`` with ``cleanup: "unknown"``; the
        recorded processes that still match their birth identity get the stop sequence. Returns what was found."""
        found = []
        for run, rec in self.table.live():
            pairs = rec.get("escapees") or []
            tracked = {p: b for p, b in pairs if isinstance(p, int) and isinstance(b, int)}
            members = self._members(rec, tracked)
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
        next_scan = time.monotonic() + self.scan_interval
        while not self._closing:
            try:
                events = self._sel.select(timeout=0.1)
            except (OSError, ValueError):
                events = []
            for key, _ in events:
                if key.data is None:
                    try:
                        while os.read(self._wake_r, 4096):
                            pass
                    except BlockingIOError:
                        pass
                    continue
                self._read(key.data)
            now = time.monotonic()
            with self.lock:
                lives = list(self.live.values())
            for live in lives:
                if live.exit is None and live.proc.poll() is not None:
                    self._leader_exit(live)
                    if not live.stopping:
                        threading.Thread(target=self._finish, args=(live, self.exit_grace), daemon=True).start()
                if live.exit is not None and not live.pty_closed and now >= live.drain_until:
                    self._close_pty(live)
            self._prune(now)
            if now >= next_scan:
                next_scan = now + self.scan_interval
                try:
                    self.scan()
                except Exception:  # noqa: BLE001 - the scan is best effort; it never stops the I/O
                    LOG.exception("escape scan failed")

    def _prune(self, now: float) -> None:
        with self.lock:
            ended = sorted((lv.ended_at, run) for run, lv in self.live.items() if lv.done.is_set() and lv.pty_closed)
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

    def _close_pty(self, live: Live) -> None:
        with self.lock:
            if live.pty_closed:
                return
            live.pty_closed = True
            try:
                self._sel.unregister(live.master)
            except (KeyError, ValueError):
                pass
            try:
                os.close(live.master)
            except OSError:
                pass

    def _leader_exit(self, live: Live) -> None:
        with self.lock:
            if live.exit is not None:
                return
            code, sig = _signame(live.proc.returncode)
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


def _spawn_error(status: dict[str, Any]) -> SpawnError:
    step, code = str(status.get("step")), status.get("errno") or 0
    if step == "cwd":
        return SpawnError("launch.cwd_changed", "the pinned working directory no longer matches")
    if step == "exec" and code in (errno.ENOENT, errno.ENOTDIR):
        return SpawnError("harness.not_found", "the program does not exist")
    reason = os.strerror(code) if isinstance(code, int) and code else "failed"
    return SpawnError("run.spawn_failed", f"the trampoline failed at step {step}: {reason}")
