"""`ctx.run` (spec v2 §11.9): argv only, allowlisted binaries, timeout, scrubbed env, never while a page renders."""
from __future__ import annotations

import contextlib
import contextvars
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from orch.errors import OrchError

_rendering: contextvars.ContextVar[bool] = contextvars.ContextVar("orch_addon_rendering", default=False)
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
            "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")
MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # per stream (stdout, stderr)
_CHUNK = 65536
_POSIX = os.name != "nt"
_LIVE_LOCK = threading.Lock()
_LIVE: set[tuple[subprocess.Popen, int | None]] = set()


def kill_live_processes() -> int:
    """Force-kill every process group a SubprocessRunner call currently has running, from any thread: used at
    shutdown so a blocked fetch (a long-poll's ctx.run can wait up to 35 s) never holds up exit — the blocked
    `proc.wait()` returns at once once its process group is dead, and the worker thread finishes right after.
    Returns how many it killed."""
    with _LIVE_LOCK:
        live = list(_LIVE)
    for proc, pgid in live:
        _kill_group(proc, pgid)
    return len(live)


class AddonRunError(OrchError):
    pass


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@contextlib.contextmanager
def rendering():
    """Mark a page render: `ctx.run` refuses inside (spec: pages read the cache only)."""
    token = _rendering.set(True)
    try:
        yield
    finally:
        _rendering.reset(token)


def is_rendering() -> bool:
    return _rendering.get()


def scrubbed_env(extra_names, environ=None) -> dict:
    environ = os.environ if environ is None else environ
    # ORCH_* is orch's own (ORCH_STATE_DIR, ORCH_HARNESS, ...); an addon never gets it, even if a
    # manifest somehow declared it (manifest_problems already refuses that at parse time).
    names = set(BASE_ENV) | {n for n in extra_names if not str(n).startswith("ORCH_")}
    return {k: v for k, v in environ.items() if k in names and not k.startswith("ORCH_")}


def _find_executable(name: str, path_value: str | None) -> str | None:
    """Only absolute directories in PATH are searched: a relative entry (`.`, `./bin`) is ignored, so
    a binary planted in the addon's or the server's current directory (e.g. `./git`) can never shadow
    the real one."""
    if os.path.isabs(name):
        return name if os.path.isfile(name) else None
    if not path_value:
        return None
    exts = [""]
    if not _POSIX:
        exts = [e for e in os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep) if e] + [""]
    for d in path_value.split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        for ext in exts:
            candidate = Path(d) / (name + ext)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _kill_group(proc: subprocess.Popen, pgid: int | None) -> None:
    """Kill the whole process group (not just `proc`), so a grandchild — e.g. one the child spawned
    and left running, inheriting the same session — dies too and releases any pipe it still holds
    open. `pgid` is captured once, right after `Popen`, because `proc.pid` may already be reaped (and
    its number reused by the OS) by the time this runs."""
    try:
        if _POSIX and pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class _ReaderThread(threading.Thread):
    """Drains one stream in the background so the child never blocks on a full pipe, stopping (and
    flagging `too_large`) once more than `cap` bytes have been read. Uses `os.read` on the raw fd
    rather than `BufferedReader.read(n)`, which blocks until either `n` bytes are available or EOF —
    exactly wrong for a child that writes a little and then goes quiet (or hangs): a short read must
    return as soon as any data arrives."""

    def __init__(self, stream, cap: int):
        super().__init__(daemon=True)
        self.stream = stream
        self.fd = stream.fileno()
        self.cap = cap
        self.chunks: list[bytes] = []
        self.total = 0
        self.too_large = threading.Event()

    def run(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self.fd, _CHUNK)
                except OSError:  # the fd was closed from the main thread to unblock us
                    break
                if not chunk:
                    break
                self.chunks.append(chunk)
                self.total += len(chunk)
                if self.total > self.cap:
                    self.too_large.set()
                    break
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


class SubprocessRunner:
    def __init__(self, *, env_names: tuple[str, ...], cwd: Path):
        self.env_names = tuple(env_names)
        self.cwd = Path(cwd)

    def __call__(self, argv, timeout: float, env: dict | None = None) -> RunResult:
        if is_rendering():
            raise AddonRunError("ctx.run is not allowed while a page renders; fetch in a provider instead")
        extra = {k: v for k, v in (env or {}).items() if k in self.env_names and not k.startswith("ORCH_")}
        env = {**scrubbed_env(self.env_names), **extra}
        exe = _find_executable(argv[0], env.get("PATH"))
        if not exe or not os.path.exists(exe):
            raise AddonRunError(f"{argv[0]} is not installed or not on PATH")
        popen_kwargs = {"start_new_session": True} if _POSIX else {}
        try:
            proc = subprocess.Popen([exe, *argv[1:]], cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs)
        except OSError as e:
            raise AddonRunError(f"{argv[0]} could not be started ({e})") from e

        pgid = None
        if _POSIX:
            try:
                pgid = os.getpgid(proc.pid)  # captured now: proc.pid may be reaped (and reused) later
            except OSError:
                pgid = None

        key = (proc, pgid)
        with _LIVE_LOCK:
            _LIVE.add(key)  # visible to kill_live_processes() from any thread until this call is done

        out_reader = _ReaderThread(proc.stdout, MAX_OUTPUT_BYTES)
        err_reader = _ReaderThread(proc.stderr, MAX_OUTPUT_BYTES)
        out_reader.start()
        err_reader.start()

        deadline = time.monotonic() + timeout
        timed_out = False
        while True:
            if out_reader.too_large.is_set() or err_reader.too_large.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                proc.wait(timeout=min(remaining, 0.2))  # polls in short slices: a kill from another thread
                break                                    # (kill_live_processes) is noticed within ~0.2 s
            except subprocess.TimeoutExpired:
                continue

        with _LIVE_LOCK:
            _LIVE.discard(key)

        too_large = out_reader.too_large.is_set() or err_reader.too_large.is_set()

        # Whatever ended the wait above (timeout, the cap, a normal exit, or an outside kill), kill the
        # process group — not only `proc` — *before* joining the readers: a reader can be blocked in
        # `os.read` with no data yet (e.g. a grandchild still holding the pipe open after the child itself
        # exited), and joining first would add that block on top of our own timeout. Killing first,
        # then closing the pipes ourselves as a backstop, makes both readers return promptly.
        _kill_group(proc, pgid)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        out_reader.join(timeout=2)
        err_reader.join(timeout=2)

        if timed_out or too_large:
            if too_large:
                raise AddonRunError("output too large")
            raise AddonRunError(f"{argv[0]} timed out after {timeout:g} s")

        stdout = out_reader.data.decode("utf-8", errors="replace")
        stderr = err_reader.data.decode("utf-8", errors="replace")
        return RunResult(tuple(argv), proc.returncode, stdout, stderr)
