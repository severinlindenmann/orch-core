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

import contextlib
import http.client
import importlib
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
TEST_BUDGET_S = 20.0  # one test, from the relay answering to its teardown (11A.10: each T2 test < 20 s)
FORCE_AFTER_S = 1.0
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
    """The relay's reference client (``ref/orch_protocol_ref.py``, test support only) from the pinned checkout."""
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    mod = importlib.import_module("ref.orch_protocol_ref")
    if not Path(mod.__file__ or "").resolve().is_relative_to(d):
        pytest.fail(f"ref imported from {mod.__file__}, not from {d}", pytrace=False)
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
        """Signal only our own process group; reap it; check it is gone and the port is closed."""
        if self.stopped:
            return
        self.stopped = True
        pgid = self.proc.pid  # start_new_session: the child leads its own group
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        end = time.monotonic() + STOP_GRACE_S
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(FORCE_AFTER_S)
        if group_alive(pgid):  # uvicorn waits for open connections; SIGINT while shutting down is its forced exit
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGINT)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(max(0.1, end - time.monotonic()))
        while group_alive(pgid) and time.monotonic() < end:
            time.sleep(0.05)
        if group_alive(pgid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
        self.proc.wait(10)
        end = time.monotonic() + 5.0
        while group_alive(pgid) and time.monotonic() < end:
            time.sleep(0.05)

    def leaked(self) -> list[str]:
        """What a stopped relay left behind (empty when clean)."""
        out = []
        if self.proc.poll() is None:
            out.append(f"child {self.proc.pid} alive")
        if group_alive(self.proc.pid):
            out.append(f"process group {self.proc.pid} alive")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex((HOST, self.port)) == 0:
                out.append(f"port {self.port} still accepting")
        return out


def start_relay(d: Path, data: Path, cap: int | None, *, attempts: int = START_ATTEMPTS) -> RelayProcess:
    """Start the pinned relay on a free port chosen first; retried on a new port if the port was taken."""
    tails = []
    for i in range(attempts):
        port = free_port()
        instance = secrets.token_hex(16)
        log = data.parent / f"{data.name}-relay-{i}.log"
        with open(log, "wb") as out:
            proc = subprocess.Popen(
                ["uv", "run", "orch-relay", "serve", "--host", HOST, "--port", str(port)], cwd=d,
                env=relay_env(data, port, instance, cap), stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, start_new_session=True,
            )  # fmt: skip
        rp = RelayProcess(d, data, port, instance, cap, proc, log)
        try:
            healthy = rp.wait_healthy()
        except BaseException:
            rp.stop()
            raise
        if healthy:
            return rp
        rp.stop()
        tails.append(rp.log_tail(1500))
    raise RuntimeError("the relay did not start: " + "\n---\n".join(tails))
