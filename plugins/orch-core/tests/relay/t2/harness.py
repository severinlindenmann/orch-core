"""T2 harness (11A.10 "Harness", layer 2): the real orch-relay as a child process on loopback.

* The relay comes from the orch-relay checkout named by ``ORCH_RELAY_DIR``, which must be at the commit pinned in
  ``tests/relay/vectors/SOURCE`` (checked with ``git rev-parse HEAD``; a mismatch fails, it never runs silently
  against another relay). Nothing of the relay is vendored into core: its ``ref`` package (the reference client) is
  imported from that checkout.
* ``ORCH_RELAY_DIR`` unset: the T2 tests SKIP with a message, unless ``CI`` or ``ORCH_RELAY_T2_REQUIRED`` is set,
  where a missing checkout FAILS (the CI job must never pass by skipping).
* Start: a free loopback port is chosen FIRST (the origin is fixed at start, so ``--port 0`` cannot work; 11A.15.12
  C4), then ``uv run orch-relay serve --host 127.0.0.1 --port <port>`` with an allow-list environment:
  ``ORCH_RELAY_ORIGIN=http://127.0.0.1:<port>``, a fresh temp ``ORCH_RELAY_DATA``, a random ``ORCH_RELAY_INSTANCE``
  and, for the internal deployment, ``ORCH_RELAY_MAX_PUBLICATION_BYTES=4194304`` (unset for the 16 MiB default).
  The relay counts as started only when ``GET /healthz`` answers OUR instance id and the child is alive; a port taken
  by another process is retried on a new port (at most 3 attempts), never adopted.
* Teardown: the child runs in its own process group (``start_new_session``); only that group is signalled
  (``SIGTERM``, then ``SIGKILL`` after 10 s), and teardown checks that the group is gone and the port is closed.
* Loopback only: every request this harness makes goes to ``127.0.0.1``. No real relay is ever contacted.
"""

from __future__ import annotations

import atexit
import contextlib
import http.client
import importlib.util
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from .. import corpus

RELAY_DIR_ENV = "ORCH_RELAY_DIR"
REQUIRED_ENVS = ("CI", "ORCH_RELAY_T2_REQUIRED")
INTERNAL_CAP = 4 * 1024 * 1024  # the internal deployment's ORCH_RELAY_MAX_PUBLICATION_BYTES
DEFAULT_CAP = 16 * 1024 * 1024  # the relay's default when the variable is unset
LIMITS = (INTERNAL_CAP, None)  # None: the variable is NOT set, the relay's own default applies
HOST = "127.0.0.1"
START_ATTEMPTS = 3
HEALTH_WAIT_S = 60.0  # a cold CI runner resolves the relay's environment on the first `uv run`
STOP_GRACE_S = 10.0
TEST_BUDGET_S = 20.0  # a hard per-test limit (11A.10: each T2 test < 20 s), from the relay answering to teardown
FORCE_AFTER_S = 1.0
_REF_NAME = "orch_relay_ref_t2"
SUPERVISOR = Path(__file__).resolve().parent / "supervisor.py"
FAKE_MARK = "orch-relay-t2-fake"  # the self-tests' stand-in child names itself with this
# the child's environment is an allow-list: nothing else of the parent leaks in (no ORCH_RELAY_* of the caller, no
# VIRTUAL_ENV of core's environment)
PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR", "UV_PYTHON", "UV_PYTHON_INSTALL_DIR",
            "UV_PYTHON_PREFERENCE", "XDG_CACHE_HOME", "XDG_DATA_HOME")  # fmt: skip


def required() -> bool:
    return any(os.environ.get(k) for k in REQUIRED_ENVS)


def pinned_commit() -> str:
    return corpus.source()["relay_commit"]


def relay_dir() -> Path:
    """The pinned orch-relay checkout, or skip (locally) / fail (CI)."""
    val = os.environ.get(RELAY_DIR_ENV, "")
    if not val:
        why = f"T2 real relay not run: {RELAY_DIR_ENV} is unset (a checkout of orch-relay {pinned_commit()[:12]})"
        if required():
            pytest.fail(why + "; this job requires it", pytrace=False)
        pytest.skip(why)
    d = Path(val).resolve()
    if not (d / "src" / "orch_relay" / "cli.py").is_file() or not (d / "ref" / "orch_protocol_ref.py").is_file():
        pytest.fail(f"{RELAY_DIR_ENV}={val} is not an orch-relay checkout", pytrace=False)
    head = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False
    ).stdout.strip()
    if head != pinned_commit():
        pytest.fail(f"{RELAY_DIR_ENV} is at {head or '?'}, SOURCE pins {pinned_commit()}", pytrace=False)
    return d


def ref_module(d: Path) -> ModuleType:
    """The relay's reference client (``ref/orch_protocol_ref.py``, test support only) from the pinned checkout, loaded
    by file path (the relay's root never goes on ``sys.path``; the module uses only the stdlib and ``cryptography``)."""
    if _REF_NAME in sys.modules:
        mod = sys.modules[_REF_NAME]
    else:
        spec = importlib.util.spec_from_file_location(_REF_NAME, d / "ref" / "orch_protocol_ref.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_REF_NAME] = mod  # dataclasses resolve their module through sys.modules
        spec.loader.exec_module(mod)
    if not Path(mod.__file__ or "").resolve().is_relative_to(d):
        pytest.fail(f"ref loaded from {mod.__file__}, not from {d}", pytrace=False)
    return mod


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def relay_env(data: Path, port: int, instance: str, cap: int | None) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env["ORCH_RELAY_ORIGIN"] = f"http://{HOST}:{port}"
    env["ORCH_RELAY_DATA"] = str(data)
    env["ORCH_RELAY_INSTANCE"] = instance
    if cap is not None:
        env["ORCH_RELAY_MAX_PUBLICATION_BYTES"] = str(cap)
    return env


def get_json(port: int, path: str, *, timeout: float = 5.0) -> tuple[int, object]:
    """A plain loopback GET (the harness's own probes; core's client code is what the tests exercise)."""
    c = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read(1 << 20)
        return r.status, json.loads(body) if body else None
    finally:
        c.close()


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # a reused id we do not own: not ours, but not gone either
        return True
    return True


def _killpg(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def group_members(pgid: int) -> list[tuple[int, float, str]]:
    """``(pid, start time (epoch s, whole seconds), command line)`` of every process in group ``pgid``."""
    r = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,lstart=,args="], capture_output=True, text=True, timeout=30, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "LANG": "C"},
    )  # fmt: skip
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[0].isdigit() or not parts[1].isdigit() or int(parts[1]) != pgid:
            continue
        try:  # lstart: "Thu Oct 15 12:53:05 2026", local time
            started = time.mktime(time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        out.append((int(parts[0]), started, parts[7]))
    return out


def spawn(cmd: list[str], *, cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    """``cmd`` under the supervisor, as the leader of a new process group (session), its stdin a pipe only this
    process holds: if this process dies in any way, the supervisor ends the group (``supervisor.py``)."""
    with open(log, "wb") as out:
        return subprocess.Popen(
            [sys.executable, str(SUPERVISOR), *cmd], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out,
            stderr=subprocess.STDOUT, start_new_session=True,
        )  # fmt: skip


def fake_cmd(port: int, *, seconds: int = 60) -> list[str]:
    """The self-tests' stand-in for the relay: a sleeper that names :data:`FAKE_MARK` and the port."""
    return [sys.executable, "-c", f"import time; time.sleep({seconds})", FAKE_MARK, "--port", str(port)]


@dataclass
class RelayProcess:
    dir: Path
    data: Path
    port: int
    instance: str
    cap: int | None
    proc: subprocess.Popen[bytes]
    log: Path
    stopped: bool = field(default=False)
    started: float = field(default_factory=time.time)  # wall clock just after the spawn (the group's birth bound)

    def __post_init__(self) -> None:
        _STARTED.append(self)

    @property
    def pgid(self) -> int:
        return self.proc.pid  # start_new_session: the supervisor leads its own group

    @property
    def origin(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def configured_cap(self) -> int:
        return DEFAULT_CAP if self.cap is None else self.cap

    def env(self) -> dict[str, str]:
        return relay_env(self.data, self.port, self.instance, self.cap)

    def log_tail(self, n: int = 4000) -> str:
        with contextlib.suppress(OSError):
            return self.log.read_text(errors="replace")[-n:]
        return ""

    def wait_healthy(self, deadline_s: float = HEALTH_WAIT_S) -> bool:
        """True once ``/healthz`` answers OUR instance id while our child is alive; False if the child exits or the
        port answers another instance (someone else's process: never adopted)."""
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                return False
            try:
                status, obj = get_json(self.port, "/healthz", timeout=2.0)
            except (OSError, http.client.HTTPException, ValueError):
                time.sleep(0.1)
                continue
            if status != 200 or not isinstance(obj, dict) or obj.get("instance") != self.instance:
                return False
            return self.proc.poll() is None
        return False

    def admin(self, *args: str, timeout: float = 60.0) -> str:
        """``uv run orch-relay <args>`` against this relay's data directory (the operator CLI)."""
        r = subprocess.run(
            ["uv", "run", "orch-relay", *args], cwd=self.dir, env=self.env(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
        if r.returncode != 0:
            raise RuntimeError(f"orch-relay {args[0]} failed ({r.returncode}): {r.stderr[-2000:]}")
        return r.stdout

    def stop(self) -> None:
        """Signal only our own process group; reap it; then close the supervisor's pipe."""
        if self.stopped:
            return
        self.stopped = True
        pgid = self.pgid
        _killpg(pgid, signal.SIGTERM)
        end = time.monotonic() + STOP_GRACE_S
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(FORCE_AFTER_S)
        if group_alive(pgid):  # uvicorn waits for open connections; SIGINT while shutting down is its forced exit
            _killpg(pgid, signal.SIGINT)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(max(0.1, end - time.monotonic()))
        while group_alive(pgid) and time.monotonic() < end:
            time.sleep(0.05)
        if group_alive(pgid):
            _killpg(pgid, signal.SIGKILL)
        self._close_pipe()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(10)
        end = time.monotonic() + 5.0
        while group_alive(pgid) and time.monotonic() < end:
            time.sleep(0.05)

    def _close_pipe(self) -> None:
        if self.proc.stdin is not None:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()

    def leaked(self) -> list[str]:
        """What a stopped relay left behind (empty when clean)."""
        out = []
        if self.proc.poll() is None:
            out.append(f"child {self.proc.pid} alive")
        if group_alive(self.pgid):
            out.append(f"process group {self.pgid} alive")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex((HOST, self.port)) == 0:
                out.append(f"port {self.port} still accepting")
        return out

    def owns_group(self) -> bool:
        """The group ``pgid`` is still the one this harness started: it has members, and every member was born after
        our spawn (whole-second clock, 2 s slack) and is our relay or supervisor for OUR port. Anything else (a
        reused pgid) is never signalled."""
        members = group_members(self.pgid)
        tag = f"--port {self.port}"
        return bool(members) and all(
            started >= int(self.started) - 2 and tag in cmd
            and ("orch-relay" in cmd or str(SUPERVISOR) in cmd or FAKE_MARK in cmd)
            for _, started, cmd in members
        )  # fmt: skip

    def kill_leaked(self) -> list[str]:
        """Reap a leak: ``SIGKILL`` the group if it is verifiably ours, then close the pipe (the supervisor's own
        backstop). Returns what had leaked."""
        leaks = self.leaked()
        if group_alive(self.pgid) and self.owns_group():
            _killpg(self.pgid, signal.SIGKILL)
        self._close_pipe()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(5)
        end = time.monotonic() + 5.0
        while group_alive(self.pgid) and self.owns_group() and time.monotonic() < end:
            time.sleep(0.05)
        return leaks

    def finish(self) -> list[str]:
        """Teardown: stop, then reap whatever leaked (SIGKILL of our own group); the leaks are returned so the caller
        FAILS on them. A group that is gone is forgotten; one still alive stays registered for :func:`reap_all`."""
        self.stop()
        leaks = self.leaked()
        if leaks:
            self.kill_leaked()
        if not group_alive(self.pgid) and self in _STARTED:
            _STARTED.remove(self)
        return leaks


_STARTED: list[RelayProcess] = []  # every group this process started; reaped at exit and at session end


def reap_all() -> list[str]:
    """Stop and reap every relay this process started that is still around; returns the leaks found."""
    leaks: list[str] = []
    while _STARTED:
        rp = _STARTED.pop()
        rp.stop()
        leaks += rp.kill_leaked()
    return leaks


atexit.register(reap_all)


def start_relay(d: Path, data: Path, cap: int | None, *, attempts: int = START_ATTEMPTS) -> RelayProcess:
    """Start the pinned relay on a free port chosen first; retried on a new port if the port was taken."""
    tails = []
    for i in range(attempts):
        port = free_port()
        instance = secrets.token_hex(16)
        log = data.parent / f"{data.name}-relay-{i}.log"
        proc = spawn(["uv", "run", "orch-relay", "serve", "--host", HOST, "--port", str(port)], cwd=d,
                     env=relay_env(data, port, instance, cap), log=log)  # fmt: skip
        rp = RelayProcess(d, data, port, instance, cap, proc, log)
        try:
            healthy = rp.wait_healthy()
        except BaseException:
            rp.stop()
            raise
        if healthy:
            return rp
        rp.stop()
        rp.kill_leaked()
        tails.append(rp.log_tail(1500))
    raise RuntimeError("the relay did not start: " + "\n---\n".join(tails))
