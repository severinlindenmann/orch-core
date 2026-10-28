"""The host's side of the runner (H11a, D-P2-24, D-P2-25, D-P2-37, D-P2-39): the runner socket, the command stream,
the host's run records and the run lifecycle reads.

**The runner socket** ``<run>/runner/host.sock`` (0700 directory, host-owned) accepts peer class ``runner`` only from
the runner unit's service-manager main PID (:func:`runner_identity`: ``launchctl print gui/<uid>/io.orch.runner``,
``systemctl --user show -p MainPID orch-runner.service``; a dev host started with ``--dev-runner`` also admits a
foreground runner announced in its run directory, :mod:`orch.runner.devmode`), and one connection per runner UID (a
second one is ``peer.denied``). Tier A: the runner UID is the host UID, so this is fault separation and hygiene, not a
boundary: same-UID separation is not containment; runner reports are untrusted and cannot establish authorization;
reports are accepted only for runs assigned to that runner (its UID and service generation). The runner checks the host
too: its connect refuses a socket whose peer UID is not the host UID (:func:`orch.host.client.connect_pinned`).

**The protocol** (cap ``runner/1``, §12.3.3). After ``welcome`` the runner sends ``runner.hello {version, gen, pid,
term_socket, runs, grants}``; the answer ``{sub, epoch}`` opens the host's command stream: ``evt`` items ``{id:"<n>.c",
cmd:ULID, op, args}``. The runner answers each with ``runner.report {cmd, outcome, result|error}`` (a successful
result is validated before anything of it is stored) and reports exits with ``runner.exit {run, code, signal, at,
cleanup}``. Commands: ``spawn``, ``stop``, ``attach.allow``, ``attach.end``. Every hello is a new **authorization
epoch**: the runner ends all attachments, drops every command of the lost connection unrun (a stale ``spawn`` never
starts after the reconnect), and accepts no new ticket until the hello's answer.

**Run records** (D-P2-37): ``<state>/hosts/<ws>/runs/<run>.json`` (0600 in 0700). The launch intent (run id, its
fields, the runner UID and generation) is written **before** the ``spawn`` command is sent; the runner dedupes
``spawn`` by run id, so a lost ack never starts a second process. ``runner.hello`` reconciles: a live record the runner
does not report becomes ``lost`` (never spawned again); a runner of another generation can only report a run of an
older generation ``lost`` or ``exited`` (as ``runner_restarted``), never ``running``; a run the host does not know is
ignored (hello is evidence only about runs the host knows). Each change takes the next ``rev``: one counter for the
whole host, persisted with the records, so it is monotonic per workspace too (§12.3.2). Ended records are kept 7 days,
at most 1 000; one that is evicted publishes ``gone``.

**Reads** (cap ``run/1``): ``run.list`` and ``run.sub`` for columns P and G (N: ``op.not_allowed``). P sees the
caller person's runs; G sees only ``kind:"agent"`` runs of its grant's person on tickets that person can see; for both
a run on a ticket the person cannot see is not shown. A client subscribes first (``run.sub`` answers ``{sub, rev}``),
then lists, and keeps the higher ``rev`` per run. Items: ``{id:"<rev>.r", t:"run", run}`` after every change and
``{t:"gone", run}`` when a run leaves the caller's view. Every delivery revalidates the principal on a fresh
authentication snapshot (as the bus does).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import math
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import frames
from ._socket import pidfd_pid_now
from .custody import ensure_private_dir, read_json, write_private
from .enable import _default_run
from .errors import HostError
from .transport import Credentials, Session

if TYPE_CHECKING:
    from .service import Host
    from .workspace import Call

__all__ = [
    "RUNNER_CAPS",
    "RUNNER_OPS",
    "RUN_CAPS",
    "RUN_OPS",
    "RunStore",
    "RunnerHub",
    "main_pid",
    "runner_identity",
    "unit_main_process",
    "system_runner_identity",
]

LOG = logging.getLogger(__name__)

RUNNER_OPS: dict[str, int] = {"runner.hello": 1, "runner.report": 1, "runner.exit": 1, "runner.term": 1}
RUNNER_CAPS = ["runner/1"]
RUN_OPS: dict[str, int] = {"run.list": 1, "run.sub": 1, "run.stop": 1}
RUN_CAPS = ["run/1"]
COMMAND_TIMEOUT = 15.0
ATTACH_SECONDS = 30
LIVE = ("starting", "running")
RUN_ID = r"^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
#: codes a runner may report for a command; anything else becomes ``run.spawn_failed`` / ``internal``
RUNNER_CODES = frozenset(
    {
        "launch.cwd_changed", "harness.not_found", "harness.shell_unsupported", "run.spawn_failed", "invalid.input",
        "not_found", "term.busy", "term.not_attachable", "attach.limit",
        "launch.no_grant", "launch.invalid",  # H11c: the runner's grant store, runner.json
        # H11d: worktree git (orch.runner.wtgit)
        "worktree.branch_in_use", "worktree.exists", "worktree.dirty", "worktree.git_failed",
        "worktree.checkout_unsupported",
        "observe.unavailable",  # the orch.gitsafe gate: the repo's own config could make git run a program
    }
)  # fmt: skip
#: ``retry_after_ms`` for the retryable codes a runner reports (§0.6: a retryable refusal says when to try again); the
#: runner's pending capabilities expire 10 s after ``attach.allow`` at the latest, most of them far sooner
RUNNER_RETRY_MS = {"attach.limit": 2000}
#: how long an exited run's ring is kept by the runner (:data:`orch.runner.supervise.KEEP_RING_SECONDS`)
REPLAY_SECONDS = 3600
WATCH_SECONDS = 0.5
#: an attachment whose person, grant or ticket cannot be decided for this long is ended (fail closed)
UNDECIDED_SECONDS = 2.0
#: how long the host waits for the runner to confirm an ``attach.end``; unanswered, the runner session is dropped
END_TIMEOUT = 5.0
#: ``term.attach`` mints per person (§0.11 style): 30 per minute, burst 10
MINT_RATE, MINT_BURST = 30 / 60, 10.0
KEEP_SECONDS = 7 * 24 * 3600
KEEP_ENDED = 1000
PER_CONNECTION = 8


def _iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


# -- the main-PID check ------------------------------------------------------------------------------------------------


def main_pid(uid: int, run: Callable[[list[str]], Any] | None = None, *, platform: str | None = None) -> int | None:
    """The runner unit's main PID from the person's service manager, or ``None``. ``run`` defaults to the H7 command
    runner, which refuses under pytest and with ``ORCH_NO_SERVICE_MANAGER=1`` (so a test never reaches launchctl)."""
    # lazy: orch.runner.service imports orch.host.enable, which imports back into the host package
    from orch.runner.service import LAUNCHD_LABEL, SYSTEMD_NAME

    run = run or _default_run
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            res = run(["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"])
            found = (
                re.search(r"^(?:\t| {4})pid = ([0-9]+)\s*$", res.stdout or "", re.M) if res.returncode == 0 else None
            )
        elif platform.startswith("linux"):
            res = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", SYSTEMD_NAME])
            found = re.fullmatch(r"\s*([0-9]+)\s*", res.stdout or "") if res.returncode == 0 else None
        else:
            return None
    except OSError:
        return None
    pid = int(found.group(1)) if found else 0
    return pid if pid > 1 else None


SYSTEMCTL = ("/usr/bin/systemctl", "/bin/systemctl")


#: the environment and time limit of the system manager query: it runs in the serialized accept loop
SYSTEMCTL_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
SYSTEMCTL_SECONDS = 3


def _system_query(argv: list[str]) -> Any:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ORCH_NO_SERVICE_MANAGER") == "1":
        raise OSError(f"{argv[0]} refused: the service manager is off (a test, or ORCH_NO_SERVICE_MANAGER=1)")
    return subprocess.run(  # noqa: S603 - fixed binary, fixed arguments, a validated unit name
        argv, capture_output=True, text=True, timeout=SYSTEMCTL_SECONDS, check=False, env=SYSTEMCTL_ENV
    )


def unit_main_process(unit: str, run: Callable[[list[str]], Any] | None = None) -> tuple[int, int] | None:
    """``(MainPID, ExecMainStartTimestampMonotonic in microseconds)`` of the SYSTEM unit ``unit`` (tier B:
    ``orch-runner@<person>.service``), asked of systemd over its D-Bus interface, which any user may read
    (``systemctl show`` is that client; no privilege and no file of the agent's is involved). ``None``: not running,
    unreadable or ambiguous (a denial)."""
    if not re.fullmatch(r"orch-runner@[A-Za-z0-9_-]{1,40}\.service", unit):
        return None
    binary = next((path for path in SYSTEMCTL if os.path.exists(path)), SYSTEMCTL[0])
    argv = [binary, "show", "-p", "MainPID", "-p", "ExecMainStartTimestampMonotonic", "--", unit]
    try:
        res = (run or _system_query)(argv)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    found = dict(line.split("=", 1) for line in (res.stdout or "").splitlines() if "=" in line)
    try:
        pid, start = int(found["MainPID"]), int(found["ExecMainStartTimestampMonotonic"])
    except (KeyError, ValueError):
        return None
    return (pid, start) if pid > 1 and start > 0 else None


def proc_start_seconds(pid: int) -> float | None:
    """When ``pid`` started, in seconds on the boot clock (``/proc/<pid>/stat`` field 22, in clock ticks)."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as handle:
            raw = handle.read(4096).decode("ascii", "replace")
        fields = raw[raw.rindex(")") + 2 :].split()  # the name (field 2) may hold spaces and parentheses
        return int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def monotonic_to_boot_offset() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


#: systemd's start stamp and the kernel's start tick are read at different moments and resolutions
START_TOLERANCE = 1.0


def started_when_systemd_says(pid: int, start_us: int) -> bool:
    """The process ``pid`` started when the unit's main process did. A reused PID has another start time. The two
    clocks differ by the time spent suspended; a suspend after the start therefore denies (fail closed), and the
    runner is then restarted by its unit. ``False`` when either time cannot be read."""
    began = proc_start_seconds(pid)
    if began is None:
        return False
    try:
        expected = start_us / 1_000_000 + monotonic_to_boot_offset()
    except (OSError, AttributeError):
        return False
    return abs(began - expected) <= START_TOLERANCE


def system_runner_identity(
    share: Any, pins: Any, credentials: Credentials, run: Callable[[list[str]], Any] | None = None
) -> bool:
    """Tier B: the connecting process is the main process of the agent's runner unit AND the root-owned pin still names
    exactly this agent user for this host (read again at every connection: a replaced pin or user never matches).

    The PID alone could be reused (a descriptor passed on, the process gone, the PID taken by the restarted runner).
    So the PID must be bound to the process: by the kernel's pidfd of the peer where there is one (Linux 6.5+), else by
    its start time against the unit's. Neither possible: denied."""
    # every refusal says which condition denied (ids and numbers only), so a CI log can tell them apart
    if credentials.uid != share.uid:
        LOG.debug("runner identity denied: peer uid %s is not the agent uid %s", credentials.uid, share.uid)
        return False
    try:
        if pins.share_for(share.host_uid) != share:
            LOG.debug("runner identity denied: the pin no longer names this agent user")
            return False
    except (OSError, ValueError) as exc:
        LOG.debug("runner identity denied: the pin cannot be trusted (%s)", type(exc).__name__)
        return False
    main = unit_main_process(share.unit, run)
    if main is None:
        LOG.debug("runner identity denied: the service manager gave no main process for %s", share.unit)
        return False
    if main[0] != credentials.pid:
        LOG.debug("runner identity denied: peer pid %s is not the unit's MainPID %s", credentials.pid, main[0])
        return False
    if credentials.pidfd_pid is not None:
        fd = credentials.pidfd
        if fd is None or credentials.pidfd_pid != credentials.pid:
            LOG.debug("runner identity denied: pidfd gave %s for peer pid %s", credentials.pidfd_pid, credentials.pid)
            return False  # the kernel refused the pidfd (an exited peer), or it names another process
        # read again now, after the manager was asked: the pidfd still names this process (``Pid:`` is -1 once it is
        # gone, which is the liveness test: a signal-0 through the pidfd would fail for another user's process)
        now = pidfd_pid_now(fd)
        if now != credentials.pid:
            LOG.debug("runner identity denied: pidfd now names %s, peer pid %s", now, credentials.pid)
            return False
        return True
    ok = started_when_systemd_says(credentials.pid, main[1])
    if not ok:
        LOG.debug(
            "runner identity denied: start time of pid %s is %s s, the unit's stamp %s us (boot offset %s)",
            credentials.pid,
            proc_start_seconds(credentials.pid),
            main[1],
            monotonic_to_boot_offset(),
        )
    return ok


def runner_identity(credentials: Credentials) -> bool:
    """The connecting process is the runner unit's main PID (the N1 rule of the worker, for the runner)."""
    pid = main_pid(credentials.uid)
    return pid is not None and pid == credentials.pid


# -- schemas -----------------------------------------------------------------------------------------------------------


def _obj(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_RUN = {"type": "string", "pattern": RUN_ID}
_INT = {"type": "integer", "minimum": 0}
_TIME = {"type": "string", "minLength": 1, "maxLength": 40}
_NULLINT = {"type": ["integer", "null"]}
#: a runner-reported time: what the runner writes (``now_iso``), never anything nested or long
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_GEN = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_ULID = {"type": "string", "pattern": "^[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_CLEANUP = {"enum": ["complete", "incomplete", "unknown"]}
#: ``exit`` of a reported run (§12.3.3): ``reason`` and ``cleanup`` may be left out
_EXIT = _obj(
    {
        "code": _NULLINT,
        "signal": {"type": ["string", "null"], "maxLength": 16},
        "at": _TIME,
        "reason": {"enum": ["exited", "stopped", "spawn_failed", "lost", "runner_restarted"]},
        "cleanup": {"enum": ["complete", "incomplete", "unknown", None]},
        "error": {"type": "string", "maxLength": 64},
    },
    ("code", "signal", "at"),
)
_REPORTED = _obj(
    {
        "run": _RUN,
        "workspace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "pid": _NULLINT,
        "birth": _NULLINT,
        "pgid": _NULLINT,
        "sid": _NULLINT,
        "cgroup": {"type": ["string", "null"], "maxLength": 1024},
        "started_at": {"type": ["string", "null"], "maxLength": 40},
        "state": {"enum": ["running", "exited", "lost"]},
        "out": _INT,
        "exit": {"anyOf": [_EXIT, {"type": "null"}]},
    },
    ("run", "workspace_id", "state"),
)
#: the runner's deposited grants (ids and times, never a secret), revalidated at hello (H11c)
_GRANT = _obj(
    {
        "workspace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "person": {"type": "string", "pattern": "^p_[0-9a-f]{32}$"},
        "grant": {"type": "string", "minLength": 1, "maxLength": 64},
        "until": _TIME,
    },
    ("workspace_id", "person", "grant", "until"),
)
RUNNER_SCHEMAS: dict[str, dict[str, Any]] = {
    "runner.hello": _obj(
        {
            "version": {"type": "string", "minLength": 1, "maxLength": 64},
            "release": {"type": "string", "minLength": 1, "maxLength": 64},
            "gen": _GEN,
            "pid": {"type": "integer", "minimum": 2},
            "term_socket": {"type": "string", "minLength": 1, "maxLength": 200},
            "runs": {"type": "array", "maxItems": 256, "items": _REPORTED},
            "grants": {"type": "array", "maxItems": 64, "items": _GRANT},
        },
        ("version", "gen", "pid", "term_socket", "runs"),
    ),
    "runner.report": _obj(
        {
            "cmd": _ULID,
            "outcome": {"enum": ["ok", "error"]},
            "result": {"type": "object"},
            "error": _obj(
                {
                    "code": {"type": "string", "minLength": 1, "maxLength": 64},
                    "message": {"type": "string", "minLength": 1, "maxLength": 400},
                },
                ("code",),
            ),
        },
        ("cmd", "outcome"),
    ),
    "runner.exit": _obj(
        {
            "run": _RUN,
            "code": _NULLINT,
            "signal": {"type": ["string", "null"], "maxLength": 16},
            "at": _TIME,
            "cleanup": _CLEANUP,
        },
        ("run", "code", "signal", "at", "cleanup"),
    ),
    "runner.term": _obj(
        {
            "run": _RUN,
            "atts": {
                "type": "array",
                "maxItems": 64,
                "items": _obj(
                    {
                        "att": {"type": "string", "pattern": "^at_[0-7][0-9A-HJKMNP-TV-Z]{25}$"},
                        "mode": {"enum": ["read", "write"]},
                        "writer": {"type": "boolean"},
                    },
                    ("att", "mode", "writer"),
                ),
            },
            "seq": _INT,
            "lease_gen": _INT,
            "out": _INT,  # the ring's end: the run printed (the "agent may need you" fallback)
            "cols": {"type": ["integer", "null"], "minimum": 20, "maximum": 500},
            "rows": {"type": ["integer", "null"], "minimum": 5, "maximum": 200},
        },
        ("run", "seq", "atts", "lease_gen"),
    ),
}
RUN_SCHEMAS: dict[str, dict[str, Any]] = {
    "run.list": _obj(
        {
            "state": {"enum": ["live", "all"]},
            "kind": {"enum": ["agent", "shell"]},
            "ticket": {"type": "string", "minLength": 1, "maxLength": 64},
            "since": _INT,
            "page": {"type": "string", "minLength": 1, "maxLength": 512},
        }
    ),
    "run.sub": _obj({}),
    "run.stop": _obj({"run": _RUN, "grace_ms": {"type": "integer", "minimum": 0, "maximum": 30000}}, ("run",)),
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in {**RUNNER_SCHEMAS, **RUN_SCHEMAS}.items()}


def validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


# -- the records -------------------------------------------------------------------------------------------------------

#: §12.3.2 ``Run``: the record's client fields with their defaults; pid, runner UID, gen and spawn payload stay here
VIEW_DEFAULTS: dict[str, Any] = {
    "run": None, "rev": 0, "kind": "shell", "person": None, "ticket": None, "harness": "shell", "mode": None,
    "model": None, "subagent_model": None, "parent_run": None, "label": None, "session": None,
    "grant": None, "grant_state": None,
    "as_person": False, "cwd": {"kind": "workspace"}, "state": "starting", "started_at": None, "viewers": 0,
    "writer": False, "cols": 120, "rows": 40, "replay_until": None,
}  # fmt: skip
#: the fields a caller of ``spawn`` may set on a new record
FIELDS = tuple(k for k in VIEW_DEFAULTS if k not in ("run", "rev", "state", "started_at", "viewers", "writer"))


def view(rec: dict[str, Any]) -> dict[str, Any]:
    out = {k: rec.get(k, d) for k, d in VIEW_DEFAULTS.items()}
    exit_ = rec.get("exit")
    if isinstance(exit_, dict):
        if exit_.get("reason") in ("exited", "stopped") and rec.get("ended_ts"):
            out["replay_until"] = _iso(float(rec["ended_ts"]) + REPLAY_SECONDS)  # the runner's ring keeps 1 h
        out["exit"] = {
            "code": exit_.get("code"), "signal": exit_.get("signal"), "at": exit_.get("at"),
            "reason": exit_.get("reason") or "exited", "cleanup": rec.get("cleanup") or "unknown",
        }  # fmt: skip
    else:
        out["exit"] = None
    return out


class RunStore:
    """The host's run records, one 0600 file per run under ``<state>/hosts/<ws>/runs/``."""

    def __init__(self, state_dir: Path, uid: int) -> None:
        self.state_dir = Path(state_dir)
        self.uid = uid
        self.records: dict[str, dict[str, Any]] = {}
        self.rev = 0

    def _dir(self, workspace_id: str) -> Path:
        return self.state_dir / "hosts" / workspace_id / "runs"

    def load(self) -> None:
        hosts = self.state_dir / "hosts"
        self.records.clear()
        if not hosts.is_dir():
            return
        cutoff = time.time() - KEEP_SECONDS
        for wdir in sorted(hosts.iterdir()):
            runs = wdir / "runs"
            if not re.fullmatch("[0-9a-f]{32}", wdir.name) or not runs.is_dir():
                continue
            for path in sorted(runs.glob("r_*.json")):
                try:
                    rec = read_json(path, self.uid)
                except HostError as exc:
                    LOG.warning("run record %s skipped: %s", path.name, exc.message)
                    continue
                if not isinstance(rec, dict) or rec.get("run") != path.stem or rec.get("workspace_id") != wdir.name:
                    continue
                if rec.get("state") not in LIVE and float(rec.get("ended_ts") or 0) < cutoff:
                    with contextlib.suppress(OSError):
                        path.unlink()
                    continue
                self.records[path.stem] = rec
                self.rev = max(self.rev, int(rec.get("rev") or 0))

    def save(self, rec: dict[str, Any]) -> None:
        wid = rec["workspace_id"]
        ensure_private_dir(self.state_dir / "hosts" / wid, self.uid)
        ensure_private_dir(self._dir(wid), self.uid)
        write_private(self._dir(wid) / f"{rec['run']}.json", rec)

    def remove(self, rec: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            (self._dir(rec["workspace_id"]) / f"{rec['run']}.json").unlink()


# -- run.sub -----------------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class RunSub:
    id: str
    session: Session
    workspace_id: str
    column: str
    person: str
    uid: int
    principal: str
    grant: str | None
    grant_id: str | None
    seen: set[str] = field(default_factory=set)
    #: the caller person's runner grant view last sent (or at registration): a ``runner`` item goes out only on a change
    last_grant: dict[str, Any] | None = None
    grant_items: int = 0
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    ended: bool = False


# -- the hub -----------------------------------------------------------------------------------------------------------


class RunnerHub:
    """The runner connection, the command stream and the run records of one host (on its event loop)."""

    def __init__(self, host: Host, *, command_timeout: float = COMMAND_TIMEOUT) -> None:
        self.host = host
        self.store = RunStore(host.state_dir, host.host_uid)
        self.command_timeout = command_timeout
        self.session: Session | None = None
        self.uid: int | None = None
        self.generation: str | None = None  # the runner's gen (§12.3.2 Gen)
        self.pid: int | None = None
        self.release: str | None = None  # the release the runner reported in its hello (``None``: an older runner)
        self.epoch = 0
        self.instance = secrets.token_hex(4)
        self.sub_id: str | None = None
        self.seq = 0
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.subs: dict[str, RunSub] = {}
        self.attachments: dict[str, dict[str, Any]] = {}
        #: the runner's last ``runner.term`` per run: {lease_gen, atts:[{att, mode, writer}]} (counts and the lease)
        self.term_state: dict[str, dict[str, Any]] = {}
        #: the newest ``runner.term`` sequence number seen per run (this runner session); older reports are ignored
        self._term_seq: dict[str, int] = {}
        #: run -> (the runner's last ``out`` offset, when the host saw it grow): local, display only, never persisted
        self.activity: dict[str, tuple[int, float]] = {}
        self._pending_pub: set[str] = set()
        self._watching: asyncio.Task[None] | None = None
        #: the watcher's running check (or end) per attachment
        self._checks: dict[str, asyncio.Task[None]] = {}
        #: starts per person (§0.11: 10 per minute, burst 5): person -> [level, at]
        self._starts: dict[str, list[float]] = {}
        #: ``term.attach`` mints per person: person -> [level, at]
        self._mints: dict[str, list[float]] = {}
        #: agent starts in progress by run id (H11c): (spec, done); a same-idem request waits for the first
        self.starting: dict[str, tuple[str, asyncio.Future[None]]] = {}
        #: ``grant.deposit`` per person (H11c): person -> [level, at]
        self._deposits: dict[str, list[float]] = {}
        self._save_lock = asyncio.Lock()
        self.ignored = 0
        from .runnergrants import RunnerGrants

        #: the runner grants (H11c): one per (workspace, person), the deposits and the forgotten ids
        self.grants = RunnerGrants(self)
        self._reported_grants: list[dict[str, Any]] = []
        #: the stale release a ``runner.restart`` was already asked for: once per release, so a runner that comes back
        #: on a release the host still calls stale is never restarted in a loop
        self._restart_asked: str | None = None

    # -- state
    @property
    def connected(self) -> bool:
        return self.session is not None and not self.session.closed

    @staticmethod
    def _host_release() -> str:
        """This host's release id (looked up at call time: ``.service`` imports this module)."""
        from .service import _release_id

        return _release_id()

    def require_current(self) -> None:
        """One rule for every start (agent, shell, spawn): the runner must run the same release as this host. A runner
        from an older install would apply its old rules (and an old environment allow-list) to new requests. The
        refusal is plain; no check is relaxed."""
        if self.release != self._host_release():
            raise HostError(
                "runner.stale",
                "the agent runner is from an older orch version; run `orch host enable` to restart it",
            )

    @property
    def term_socket(self) -> Path:
        return (
            Path(self.host.term_base)
            / "term"
            / str(self.uid if self.uid is not None else self.host.host_uid)
            / "term.sock"
        )

    def load(self) -> None:
        self.store.load()

    async def _save(self, rec: dict[str, Any], *, publish: bool = True) -> dict[str, Any]:
        async with self._save_lock:
            if rec.get("state") not in LIVE and "ended_ts" not in rec:
                rec["ended_ts"] = time.time()
            self.store.records[rec["run"]] = rec
            # an evicted record's ``gone`` takes a fresh rev (a client keeps the higher rev per run, so the record's
            # old rev would be ignored); the saved record takes the next one, so the persisted maximum covers both
            gones = []
            for old in self._evict():
                self.store.rev += 1
                gones.append({**old, "rev": self.store.rev, "_gone": True})
            self.store.rev += 1
            rec["rev"] = self.store.rev
            if rec["run"] in self.store.records:  # never write back a record its own save just evicted
                await asyncio.to_thread(self.store.save, dict(rec))
        for gone in gones:
            await asyncio.to_thread(self.store.remove, gone)
            self.publish(gone)
        if publish:
            self.publish(rec)
        return rec

    def _evict(self) -> list[dict[str, Any]]:
        """Ended records past retention (7 days, at most 1 000): removed, each publishes ``gone``."""
        ended = sorted(
            (float(r.get("ended_ts") or 0), run) for run, r in self.store.records.items() if r.get("state") not in LIVE
        )
        cutoff = time.time() - KEEP_SECONDS
        drop = [run for ts, run in ended if ts < cutoff]
        drop += [run for _, run in ended[: max(0, len(ended) - KEEP_ENDED)] if run not in drop]
        return [self.store.records.pop(run) for run in drop]

    # -- the runner socket gate
    def gate(self, session: Session, frame: dict[str, Any], request: frames.Request) -> Any:
        op = frame["op"]

        def refuse(error: HostError) -> None:
            session.respond(request, error.response(request.id))

        if session.peer.kind != "runner":
            refuse(HostError("op.not_allowed", "runner ops are for the runner"))
            return None
        if op not in RUNNER_OPS:
            refuse(HostError("unknown_command", f"{op} is not a runner op"))
            return None
        if self.host.draining:
            refuse(self.host.restarting())
            return None
        if "auth" in frame:
            refuse(HostError("req.invalid", "auth is not allowed on the runner socket"))
            return None
        if not session.admit(request):
            return None
        return self._work(session, frame, request)

    async def _work(self, session: Session, frame: dict[str, Any], request: frames.Request) -> None:
        op, args = frame["op"], frame["args"]
        try:
            validate(op, args)
            if not session.state.start(request, "write"):
                return
            if op == "runner.hello":
                result = await self.hello(session, args)
            else:
                if session is not self.session:
                    raise HostError("op.not_allowed", "runner.hello first")
                if op == "runner.report":
                    result = await self.report(args)
                elif op == "runner.term":
                    result = await self.term_report(session, args)
                else:
                    result = await self.exit(session, args)
            from .workspace import ok_result

            session.respond(request, {"t": "res", "id": request.id, "ok": True, "result": ok_result(result)})
            if op == "runner.hello":
                self._after_hello()
        except HostError as exc:
            session.respond(request, exc.response(request.id))

    # -- runner ops
    def expected_term_socket(self, uid: int) -> Path:
        """§12.6.1: the only term socket path the host accepts for a runner UID."""
        return Path(self.host.term_base) / "term" / str(uid) / "term.sock"

    async def hello(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        if self.session is not None and self.session is not session and not self.session.closed:
            raise HostError("peer.denied", "another runner connection is live")
        if self.session is session:
            raise HostError("req.invalid", "runner.hello was already sent on this connection")
        uid = session.peer.uid
        if args["pid"] != session.credentials.pid:  # the PID the main-PID check accepted, nothing else
            raise HostError("invalid.input", "runner.hello: pid is not the connecting process")
        if args["term_socket"] != str(self.expected_term_socket(uid)):
            raise HostError("invalid.input", "runner.hello: term_socket is not <run>/term/<uid>/term.sock")
        previous = self.generation
        self.session, self.uid, self.generation, self.pid = session, uid, args["gen"], args["pid"]
        self.release = args.get("release")
        self.epoch += 1
        self._term_seq.clear()  # a new runner process counts its reports from 1 again
        self.sub_id = "sub_" + secrets.token_hex(8)
        session.subscriptions += 1  # the command stream keeps the connection out of the idle close
        session.on_close.append(lambda: self.disconnected(session))
        reported = {r["run"]: r for r in args["runs"]}
        for run, rec in list(self.store.records.items()):
            if rec.get("runner_uid") != uid:
                continue
            rep = reported.get(run)
            if rep is not None and rep.get("workspace_id") != rec.get("workspace_id"):
                rep = None  # a report that names another workspace is no report about this run
            if rec.get("state") in LIVE:
                await self._apply(rec, rep)
            elif rep is not None and rec.get("cleanup") in (None, "incomplete"):
                cleanup = (rep.get("exit") or {}).get("cleanup")
                if cleanup and cleanup != rec.get("cleanup"):
                    await self._save({**rec, "cleanup": cleanup})
        unknown = sum(1 for run in reported if run not in self.store.records)
        if unknown:
            LOG.warning("runner.hello reported %d run(s) this host does not know; not adopted", unknown)
        # grants (§12.3.3): revalidated right after the answer (_after_hello), never adopted as they are
        self._reported_grants = list(args.get("grants") or [])
        LOG.info(
            "runner connected: uid %s gen %s epoch %s (previous gen %s)", uid, self.generation, self.epoch, previous
        )
        return {"sub": self.sub_id, "epoch": f"{self.instance}.{self.epoch}"}

    def _after_hello(self) -> None:
        """After the hello's answer is queued: the reported grants are revalidated (H11c): what the host no longer
        allows is dropped at the runner, and the runner's word settles ``current`` and pending deposits."""
        reported, self._reported_grants = self._reported_grants, []
        asyncio.get_running_loop().create_task(self._reconcile_grants(reported))
        self._restart_if_stale()

    def _restart_if_stale(self) -> None:
        """Tier B (the runner's UID is not the host's): the person cannot restart the runner's system unit, so a runner
        that loaded another release than this host is asked to restart when idle (``runner.restart``, spec 13.2 item 7);
        ``Restart=always`` brings it back on ``<release>/current``. Tier A restarts through the person's service
        manager (:func:`orch.runner.service.restart_if_stale`), never here."""
        mine = self._host_release()
        if self.uid is None or self.uid == self.host.host_uid or self.release == mine:
            return
        if self._restart_asked == mine:
            LOG.warning("the runner still runs another release after a restart; not asking again")
            return
        self._restart_asked = mine
        asyncio.get_running_loop().create_task(self._ask_restart(False))

    async def _ask_restart(self, force: bool) -> None:
        try:
            await self.restart_runner(force=force)
        except HostError as exc:
            LOG.warning("runner.restart was not accepted: %s", exc.code)

    async def restart_runner(self, *, force: bool = False) -> dict[str, Any]:
        """Ask the connected runner to end when it has no live runs (or at once with ``force``) and come back through
        its service manager on the current release. ``{"restarting": True, "live": n, "force": bool}``."""
        return await self.command("restart", {"force": bool(force)}, timeout=10)

    async def _reconcile_grants(self, reported: list[dict[str, Any]]) -> None:
        try:
            await self.grants.reconcile(reported)
        except Exception as exc:  # noqa: BLE001 - logged by type only; the watcher and the next hello retry
            LOG.warning("runner grant reconciliation failed: %s", type(exc).__name__)

    async def _apply(self, rec: dict[str, Any], rep: dict[str, Any] | None) -> None:
        """Reconcile one live record with the runner's report of it (``None``: not reported)."""
        if rep is None:  # never spawned again: the runner has no record of it (D-P2-37)
            await self._save({**rec, "state": "lost", "cleanup": "unknown", "exit": _exit("lost")})
            return
        state = rep["state"]
        new = dict(rec)
        if state == "running" and rec.get("gen") != self.generation:
            # a runner of another generation cannot be running a run of an older one (its crash sweep ended it)
            await self._save({**rec, "state": "lost", "cleanup": "unknown", "exit": _exit("runner_restarted")})
            return
        if state == "running":
            new.update(state="running", pid=rep.get("pid"), out=rep.get("out", rec.get("out")),
                       started_at=rep.get("started_at") or rec.get("started_at"))  # fmt: skip
            if new != rec:
                await self._save(new)
            return
        reported_exit = rep.get("exit") or {}
        exit_ = {k: reported_exit.get(k) for k in ("code", "signal", "at")}
        exit_["at"] = exit_["at"] or _iso()
        cleanup = reported_exit.get("cleanup") or "unknown"
        if state == "lost":
            exit_["reason"] = "runner_restarted" if rec.get("gen") != self.generation else "lost"
            new.update(state="lost", exit=exit_, cleanup=cleanup)
        else:
            exit_["reason"] = reported_exit.get("reason") or ("stopped" if rec.get("stopping") else "exited")
            new.update(state="exited", exit=exit_, cleanup=reported_exit.get("cleanup"))
        await self._save(new)

    async def report(self, args: dict[str, Any]) -> dict[str, Any]:
        future = self.pending.pop(args["cmd"], None)
        if future is None or future.done():
            raise HostError("not_found", "no such command")
        if args["outcome"] == "ok":
            future.set_result(args.get("result") or {})
        else:
            err = args.get("error") or {}
            code = err.get("code") if err.get("code") in RUNNER_CODES else "run.spawn_failed"
            message = str(err.get("message") or code)[:400]
            future.set_exception(HostError(code, message, retry_after_ms=RUNNER_RETRY_MS.get(code)))
        return {}

    async def exit(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        """``runner.exit {run, code, signal, at, cleanup}``: the leader ended and the clean-up has a result (a later
        one may follow when an incomplete clean-up completes). Only for runs of this runner UID and generation."""
        rec = self.store.records.get(args["run"])
        if rec is None or rec.get("runner_uid") != session.peer.uid or rec.get("gen") != self.generation:
            raise HostError("not_found", "no such run on this runner")
        new = dict(rec)
        if (rec.get("exit") or {}).get("pending"):  # the stop answer came first: now the leader's real status
            new["exit"] = {"code": args["code"], "signal": args["signal"], "at": args["at"], "reason": "stopped"}
        if rec.get("state") in LIVE:
            reason = "stopped" if rec.get("stopping") else "exited"
            new.update(state="exited", exit={"code": args["code"], "signal": args["signal"], "at": args["at"],
                                             "reason": reason})  # fmt: skip
        if rec.get("cleanup") != args["cleanup"] and rec.get("cleanup") in (None, "incomplete"):
            new["cleanup"] = args["cleanup"]
        if new != rec:
            await self._save(new)
        return {}

    async def term_report(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        """``runner.term``: a run's attachments, lease and size changed. Only for runs of this runner (UID and
        generation). It sets ``viewers``, ``writer``, ``cols`` and ``rows`` of the run and keeps the counts the limits
        read; it never decides authorization (the host's own table and the capability do)."""
        run = args["run"]
        rec = self.store.records.get(run)
        if rec is None or rec.get("runner_uid") != session.peer.uid or rec.get("gen") != self.generation:
            raise HostError("not_found", "no such run on this runner")
        if args.get("out") is not None and args["out"] != (self.activity.get(run) or (None, 0))[0]:
            self.activity[run] = (args["out"], time.time())
        if args["seq"] <= self._term_seq.get(run, -1):
            return {}  # a stale snapshot overtaken by a newer one: it must never take a live attachment out (D2)
        self._term_seq[run] = args["seq"]
        atts = args["atts"]
        listed = {a["att"] for a in atts}
        for att in listed - self.attachments.keys():  # the runner holds only what the host's table holds
            asyncio.get_running_loop().create_task(self._end_unknown(att))
        if self.attachments:
            self._watch_attachments()  # a watcher that stopped for any reason is started again
        for att, info in list(self.attachments.items()):
            if info["run"] == run and att in listed:
                info["seen"] = True
            elif info["run"] == run and info.get("seen"):  # the runner ended it (this report is the newest)
                self.attachments.pop(att, None)
                self._audit("term.detach", run=run, att=att, person=info.get("person"))
        previous = (self.term_state.get(run) or {}).get("atts") or []
        old_writer = next((a["att"] for a in previous if a["writer"]), None)
        new_writer = next((a["att"] for a in atts if a["writer"]), None)
        if new_writer is not None and old_writer is not None and new_writer != old_writer:
            self._audit("term.takeover", run=run, old=old_writer, new=new_writer)
        self.term_state[run] = {"lease_gen": args["lease_gen"], "atts": atts}
        new = dict(rec)
        new["viewers"] = len(atts)
        new["writer"] = any(a["writer"] for a in atts)
        for key in ("cols", "rows"):
            if args.get(key) is not None:
                new[key] = args[key]
        if any(new.get(k) != rec.get(k) for k in ("viewers", "writer", "cols", "rows")):
            await self._save(new, publish=False)
            self._publish_later(run)  # at most one such item per run per 250 ms (§12.3.2)
        return {}

    def _publish_later(self, run: str) -> None:
        if run in self._pending_pub:
            return
        self._pending_pub.add(run)

        def fire() -> None:
            self._pending_pub.discard(run)
            rec = self.store.records.get(run)
            if rec is not None:
                self.publish(rec)

        asyncio.get_running_loop().call_later(0.25, fire)

    def _audit(self, event: str, **fields: Any) -> None:
        """One line of the security audit (never terminal content)."""
        from . import securitylog

        securitylog.record(self.host.state_dir, self.host.host_uid, time.time(), event, **fields)

    def counts(self, run: str, person: str) -> tuple[int, int, int]:
        """Attachments the runner reported: on the run, of the person, on the runner (pending capabilities count
        toward none, §12.6.2)."""
        per_run = per_person = total = 0
        for r, state in self.term_state.items():
            for a in state["atts"]:
                total += 1
                per_run += r == run
                per_person += (self.attachments.get(a["att"]) or {}).get("person") == person
        return per_run, per_person, total

    def take_start(self, person: str) -> bool:
        """The start rate of §0.11: 10 per minute per person, burst 5."""
        return _take(self._starts, person, 10 / 60, 5.0)

    def start_retry_ms(self, person: str) -> int:
        """After a refused :meth:`take_start`: the milliseconds until the person's next start token."""
        return _retry_ms(self._starts, person, 10 / 60)

    def mint_retry_ms(self, person: str) -> int:
        """After a refused :meth:`take_mint`: the milliseconds until the person's next ``term.attach`` token."""
        return _retry_ms(self._mints, person, MINT_RATE)

    def take_mint(self, person: str) -> bool:
        """The ``term.attach`` rate: 30 mints per minute per person, burst 10 (each one is an audit line, a host
        table entry and a runner capability)."""
        return _take(self._mints, person, MINT_RATE, MINT_BURST)

    def disconnected(self, session: Session) -> None:
        if self.session is not session:
            return
        self.session = None
        self.sub_id = None
        for wid in {s.workspace_id for s in self.subs.values()}:
            self.publish_grant(wid)  # no connected runner: every person's grant reads null now
        for future in self.pending.values():
            if not future.done():
                future.set_exception(HostError("outcome.unknown", "the runner disconnected before it answered"))
        self.pending.clear()
        self.attachments.clear()
        self.term_state.clear()
        self._term_seq.clear()
        LOG.warning("runner disconnected (uid %s)", self.uid)

    # -- commands
    async def command(self, cmd: str, args: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        session = self.session
        if session is None or session.closed or self.sub_id is None:
            raise HostError("runner.unavailable", "no runner is connected", retry_after_ms=1000)
        cid = new_ulid()  # the command id the runner's report names (`runner.report {cmd}`, §12.3.3)
        self.seq += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[cid] = future
        item = {"id": f"{self.seq}.c", "cmd": cid, "op": cmd, "args": args}
        try:
            session.send({"t": "evt", "sub": self.sub_id, "cursor": str(self.seq), "items": [item]})
        except frames.FrameError as exc:
            self.pending.pop(cid, None)
            session.failure = exc
            raise HostError("runner.unavailable", "the runner connection is overloaded", retry_after_ms=1000) from None
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.command_timeout if timeout is None else timeout)
        except TimeoutError:
            self.pending.pop(cid, None)
            raise HostError("outcome.unknown", f"the runner did not answer {cmd} in time") from None

    async def spawn(self, workspace_id: str, fields: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Start a run: the launch intent is durable before ``spawn`` is sent (D-P2-37). ``fields`` are the record's
        client-visible fields (kind, person, ticket, harness, mode, model, session, label); ``payload`` is the spawn
        command (argv, env_fixed, env_names, cwd pin, cols, rows). A lost ack leaves the run ``starting`` until the
        next ``runner.hello`` reconciles it; it is never spawned twice."""
        if not self.connected:
            raise HostError("runner.unavailable", "no runner is connected", retry_after_ms=1000)
        self.require_current()
        run = fields.get("run") or new_run_id()
        if run in self.store.records:
            return view(self.store.records[run])
        await self.intent(workspace_id, {**fields, "run": run}, payload)
        return await self.dispatch(run, payload)

    async def intent(
        self, workspace_id: str, fields: dict[str, Any], payload: dict[str, Any], **audit: Any
    ) -> dict[str, Any]:
        """The launch intent (D-P2-37), durable before anything is created or sent: state ``starting``, bound to this
        runner's UID and generation. A host restart before the spawn's ack reconciles it at ``runner.hello`` (a run
        the runner does not report becomes ``lost``, never spawned again)."""
        run = fields["run"]
        rec = {
            **{k: fields.get(k, VIEW_DEFAULTS[k]) for k in FIELDS},
            "cols": payload.get("cols", VIEW_DEFAULTS["cols"]),
            "rows": payload.get("rows", VIEW_DEFAULTS["rows"]),
            "run": run,
            "workspace_id": workspace_id,
            "state": "starting",
            "exit": None,
            "cleanup": None,
            "created_at": _iso(),
            "runner_uid": self.uid,
            "gen": self.generation,
            "dispatched": True,
        }
        for extra in ("policy", "parent_run", "spawned_by"):  # P2-3: the spawn policy and the human-delegated link
            if fields.get(extra) is not None:
                rec[extra] = fields[extra]
        if audit.get("spec"):  # an agent start: its idem names exactly this spec (a retry with another is a conflict)
            rec["spec"] = audit["spec"]
        saved = await self._save(rec)
        if audit:
            self._audit("run.intent", run=run, workspace_id=workspace_id, person=fields.get("person"), **audit)
        return saved

    async def fail(self, run: str, code: str) -> None:
        """A launch that failed before its ``spawn`` was sent (a worktree that could not be created)."""
        rec = self.store.records.get(run)
        if rec is not None and rec.get("state") in LIVE:
            await self._save({**rec, "state": "exited", "exit": _exit("spawn_failed", code), "cleanup": "complete"})

    async def dispatch(self, run: str, payload: dict[str, Any], *, audit: bool = False) -> dict[str, Any]:
        """Send ``spawn`` for a recorded intent and settle the record from the runner's answer."""
        workspace_id = self.store.records[run]["workspace_id"]
        if audit:
            self._audit("run.dispatch", run=run, workspace_id=workspace_id)
        try:
            res = await self.command("spawn", {**payload, "run": run, "workspace_id": workspace_id})
        except HostError as exc:
            if exc.code in ("outcome.unknown", "runner.unavailable"):
                if audit:
                    self._audit("run.outcome", run=run, outcome="unknown")
                raise HostError(
                    "outcome.unknown", f"the spawn of {run} was sent; the runner did not confirm it"
                ) from None
            await self._save({**self.store.records[run], "state": "exited", "exit": _exit("spawn_failed", exc.code)})
            if audit:
                self._audit("run.outcome", run=run, outcome="spawn_failed", code=exc.code)
            if exc.code == "launch.no_grant" and payload.get("kind") == "agent" and not payload.get("run_grant"):
                # the runner's word is the truth: it holds no grant the host believed current. Forget it, so the
                # person sees the normal "give agent access" step instead of a dead end
                person = self.store.records[run].get("person")
                if person and payload.get("grant"):
                    await self.grants.forget_stale(workspace_id, person, payload["grant"])
                raise HostError(
                    "launch.no_grant",
                    "Agent access on this computer was lost, for example after a restart. Give agent access again, "
                    "then start.",
                    detail={"reason": "none"},
                ) from None
            raise
        cur = self.store.records[run]
        pid = res.get("pid") if isinstance(res.get("pid"), int) and not isinstance(res.get("pid"), bool) else None
        pid = pid if pid is not None and pid >= 2 else None
        started = res.get("started_at")
        started = started if isinstance(started, str) and _TIME_RE.fullmatch(started) else _iso()
        if cur.get("state") == "starting":
            # running until the runner's exit report says otherwise (that report carries the exit itself)
            await self._save({**cur, "state": "running", "pid": pid, "started_at": started})
        if audit:
            self._audit("run.outcome", run=run, outcome="started", pid=pid)
        return view(self.store.records[run])

    async def stop(self, run: str, grace_ms: int = 10_000) -> dict[str, Any]:
        """Stop a run; the live runs a person delegated from it (``parent_run``) stop with it (P2-3)."""
        kids = [
            r["run"] for r in self.store.records.values()
            if r.get("parent_run") == run and r.get("spawned_by") == "human" and r.get("state") in LIVE
        ]  # fmt: skip
        out = await self._stop_one(run, grace_ms)
        for kid in kids:
            try:
                await self._stop_one(kid, grace_ms)
            except HostError:
                pass  # the parent's answer is the caller's; a child that would not stop shows as running
        return out

    async def _stop_one(self, run: str, grace_ms: int = 10_000) -> dict[str, Any]:
        rec = self.store.records.get(run)
        if rec is None:
            raise HostError("not_found", "no such run")
        if rec.get("state") not in LIVE:
            return {"state": rec.get("state"), "cleanup": rec.get("cleanup") or "unknown"}
        if not rec.get("stopping"):
            rec = await self._save({**rec, "stopping": True}, publish=False)  # the exit report then says "stopped"
        try:
            res = await self.command("stop", {"run": run, "grace_ms": grace_ms}, timeout=grace_ms / 1000 + 10)
        except HostError:
            cur = self.store.records.get(run) or rec
            if cur.get("stopping") and cur.get("state") in LIVE:  # nothing was stopped: a later exit is "exited"
                await self._save({**cur, "stopping": False}, publish=False)
            raise
        cur = self.store.records[run]
        cleanup = res.get("cleanup") if res.get("cleanup") in ("complete", "incomplete", "unknown") else "unknown"
        # the runner's word decides whether the leader ended: a leader that survived the stop stays running
        ended = res.get("state") in ("exited", "lost")
        new = {**cur, "cleanup": cleanup}
        if ended and cur.get("state") in LIVE:
            new.update(state="exited", exit=cur.get("exit") or {**_exit("stopped"), "pending": True})
        if new != cur:
            await self._save(new)
        state = new.get("state")
        return {"state": "exited" if state not in LIVE else "running", "cleanup": cleanup}

    async def attach_allow(
        self,
        run: str,
        person: str,
        mode: str,
        *,
        take: bool = False,
        lease_gen: int = 0,
        session: str | None = None,
        start_from: int | None = None,
        who: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mint a single-use attach capability (§12.6.2) and register its hash at the runner before answering.
        Returns ``{att, ticket, nonce, expires_at, socket, runner:{uid, pid, gen}, start, offset, cols, rows,
        lease_gen}``; the host keeps only the hash. ``who``: what the watcher needs to revalidate the person
        (``column``, ``uid``, ``grant``, ``grant_id``)."""
        if mode not in ("read", "write"):
            raise HostError("invalid.input", "mode is read or write")
        rec = self.store.records.get(run)
        if rec is None:
            raise HostError("not_found", "no such run")
        att = "at_" + new_ulid()
        ticket = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")  # 43 characters
        nonce = secrets.token_hex(16)
        expires_at = _iso(time.time() + ATTACH_SECONDS)
        now = time.monotonic()
        self._purge_unpresented()
        # recorded before the command: an end that races the answer still finds it (the runner remembers a cancel)
        self.attachments[att] = {"run": run, "person": person, "mode": mode, "workspace_id": rec["workspace_id"],
                                 "ticket": rec.get("ticket"), "at": now, **(who or {})}  # fmt: skip
        try:
            got = await self.command(
                "attach.allow",
                {"att": att, "ticket_hash": _hash(ticket), "workspace_id": rec["workspace_id"], "person": person,
                 "run": run, "session": session, "mode": mode, "take": take, "lease_gen": lease_gen,
                 "runner_gen": self.generation, "from": start_from, "nonce": nonce, "expires_at": expires_at},
            )  # fmt: skip
        except HostError:
            self.attachments.pop(att, None)
            raise
        self._audit("term.attach", run=run, att=att, person=person, mode=mode, take=take)
        self._watch_attachments()

        def num(key: str, low: int, high: int | None = None) -> int:
            v = got.get(key)
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= low and (high is None or v <= high)
            if not ok:
                raise HostError("outcome.unknown", f"the runner's answer to attach.allow has no valid {key}")
            return v

        return {
            "att": att, "ticket": ticket, "nonce": nonce, "expires_at": expires_at, "socket": str(self.term_socket),
            "runner": {"uid": self.uid, "pid": self.pid, "gen": self.generation},
            "start": num("start", 0), "offset": num("offset", 0), "cols": num("cols", 20, 500),
            "rows": num("rows", 5, 200), "lease_gen": num("lease_gen", 0),
        }  # fmt: skip

    async def attach_end(self, att: str, reason: str) -> None:
        """End an attachment at the runner. The host's entry is removed only once the runner confirmed the end
        (invariant: **the runner holds only attachments the host's table holds**); until then it is marked
        ``ending`` and the watcher sends the end again on every pass. An end the runner does not confirm drops the
        runner session: the runner's ``host_lost`` then ends every attachment (fail closed)."""
        info = self.attachments.get(att)
        if info is not None and "ending" not in info:
            info["ending"] = reason
            self._audit("term.end", run=info.get("run"), att=att, person=info.get("person"), reason=reason)
        await self._send_end(att, info["ending"] if info is not None else reason)
        self.attachments.pop(att, None)

    async def _send_end(self, att: str, reason: str) -> None:
        session = self.session
        try:
            await self.command("attach.end", {"att": att, "reason": reason}, timeout=END_TIMEOUT)
        except HostError as exc:
            if session is not None and session is self.session and not session.closed:
                LOG.warning("the runner did not confirm attach.end (%s): dropping its session", exc.code)
                self.drop_session(session)
            raise

    async def _end_unknown(self, att: str) -> None:
        """An attachment the runner reports and the host does not hold: end it there."""
        with contextlib.suppress(HostError):
            await self._send_end(att, "cancelled")

    def drop_session(self, session: Session) -> None:
        """Close the runner's connection (its ``host_lost`` ends every attachment and refuses capabilities until the
        next hello); the host forgets its attachments at once."""
        if session.failure is None:
            session.failure = frames.FrameError("internal")
        self.disconnected(session)

    # -- the push path: the host ends an attachment the moment its person or ticket no longer qualifies
    def _watch_attachments(self) -> None:
        if self._watching is None or self._watching.done():
            self._watching = asyncio.get_running_loop().create_task(self._watch())

    async def _watch(self) -> None:
        """One pass every ``WATCH_SECONDS``; the pass itself never waits on a workspace worker. Each attachment's
        check runs as its own task, bounded by ``UNDECIDED_SECONDS`` (a timeout, an exception or an undecidable
        snapshot all count as undecided), so one stalled workspace never delays another. An attachment whose last
        *decided* check (``decided_at``) is older than ``UNDECIDED_SECONDS + WATCH_SECONDS`` is ended
        (``host.restarting``, fail closed), whether its check failed, timed out or still hangs. Restarted by every mint
        and every ``runner.term`` while attachments exist."""
        with contextlib.suppress(asyncio.CancelledError):
            while self.attachments:
                await asyncio.sleep(WATCH_SECONDS)
                self._watch_pass()

    def _watch_pass(self) -> None:
        self._purge_unpresented()
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        for att in [a for a, t in self._checks.items() if t.done()]:
            del self._checks[att]
        for att, info in list(self.attachments.items()):
            task = self._checks.get(att)
            busy = task is not None and not task.done()
            if "ending" in info:
                if not busy:  # an end the runner has not confirmed yet: again
                    self._checks[att] = loop.create_task(self._end_quietly(att, info["ending"]))
            elif now - info.get("decided_at", info["at"]) > UNDECIDED_SECONDS + WATCH_SECONDS:
                if busy:
                    task.cancel()  # a check still hanging on its worker: nobody confirmed it may continue
                self._checks[att] = loop.create_task(self._end_quietly(att, "host.restarting"))
            elif not busy:
                self._checks[att] = loop.create_task(self._check(att, info))

    async def _end_quietly(self, att: str, reason: str) -> None:
        with contextlib.suppress(HostError):  # attach_end already dropped the runner session when unconfirmed
            await self.attach_end(att, reason)

    async def _check(self, att: str, info: dict[str, Any]) -> None:
        started = time.monotonic()
        try:
            reason = await asyncio.wait_for(self._ended_reason(info), UNDECIDED_SECONDS)
        except (_Undecided, TimeoutError):
            return  # undecided: the deadline in _watch_pass ends it if this lasts
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the watcher must outlive any single failure
            LOG.warning("term watcher cannot decide on an attachment: %s", type(exc).__name__)
            return
        if reason is None:
            info["decided_at"] = max(info.get("decided_at", info["at"]), started)
            return
        try:
            await self._end_quietly(att, reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.warning("term watcher: %s", type(exc).__name__)

    def _purge_unpresented(self) -> None:
        """Forget capabilities minted and never presented (long expired at the runner)."""
        now = time.monotonic()
        for old, info in list(self.attachments.items()):
            if not info.get("seen") and "ending" not in info and now - info["at"] > ATTACH_SECONDS + 10:
                del self.attachments[old]

    async def _ended_reason(self, info: dict[str, Any]) -> str | None:
        """Why this attachment must end now (``runner.disabled``, ``person.removed``, ``grant.expired``,
        ``not_visible``), else ``None``; :class:`_Undecided` when that cannot be told yet."""
        from types import SimpleNamespace

        from .terms import missing

        service = self.host.services.get(info["workspace_id"])
        auth = getattr(service, "auth", None)
        if service is None or auth is None:
            return "not_visible"
        # D-P2-31's kill switches end live attachments too: the addon revoked or disabled, the machine switch or
        # the terminals switch off
        if await asyncio.wrap_future(service.submit(missing, service, self)) is not None:
            return "runner.disabled"
        if not auth.fresh(service.entry.store_dir):
            await asyncio.wrap_future(service.submit(lambda s=service: (s.refresh(), s.sync(check=True))))
            auth = getattr(service, "auth", None)
        if auth is None or auth.stamp is None or not auth.fresh(service.entry.store_dir):
            raise _Undecided
        pseudo = SimpleNamespace(column=info.get("column", "P"), person=info["person"], uid=info.get("uid", -1),
                                 workspace_id=info["workspace_id"], grant=info.get("grant"),
                                 grant_id=info.get("grant_id"))  # fmt: skip
        reason = self.host.bus.principal_end(pseudo, auth, service.clock())  # type: ignore[arg-type]
        if reason is not None:
            return reason
        if info.get("ticket"):
            sub = RunSub("", None, info["workspace_id"], "P", info["person"], 0, "", None, None)  # type: ignore[arg-type]
            if not await asyncio.wrap_future(service.submit(_ticket_visible, service, sub, info["ticket"])):
                return "not_visible"
        return None

    # -- reads
    def visible(self, rec: dict[str, Any], column: str, person: str) -> bool:
        """P: the caller person's runs; G: that person's agent runs on a ticket (the ticket's visibility to the grant's
        person, i.e. the grant's reach, is checked by the caller); N and every other column: nothing."""
        if rec.get("person") != person:
            return False
        if column == "P":
            return True
        return column == "G" and rec.get("kind") == "agent" and bool(rec.get("ticket"))

    def runner_state(self, workspace_id: str, person: str | None = None) -> dict[str, Any]:
        from orch.runner import enablement

        state = enablement.state_of(self.host.state_dir, workspace_id, self.host.host_uid)
        cur = self.grants.current(workspace_id, person) if person else None
        grant = {"id": cur["grant"], "until": cur["until"], "scope": cur["scope"]} if cur else None
        return {"connected": self.connected, "enabled": state["enabled"], "terminals": state["terminals"],
                "gen": self.generation if self.connected else None, "grant": grant}  # fmt: skip

    def grant_view(self, workspace_id: str, person: str) -> dict[str, Any] | None:
        """The person's runner grant as ``whoami.runner.grant`` shows it (never the secret); ``None`` without one or
        without a connected runner."""
        cur = self.grants.current(workspace_id, person)
        return {"id": cur["grant"], "until": cur["until"], "scope": cur["scope"]} if cur else None

    def publish_grant(self, workspace_id: str, person: str | None = None) -> None:
        """A runner grant changed (or may have): wake the ``run.sub`` subscribers of that person (``None``: every
        person) in the workspace; each delivery sends a ``runner`` item only if its own person's view differs."""
        for sub in list(self.subs.values()):
            if sub.workspace_id == workspace_id and not sub.ended and (person is None or sub.person == person):
                sub.queue.put_nowait({"_runner": True})

    def publish(self, rec: dict[str, Any]) -> None:
        for sub in list(self.subs.values()):
            if sub.workspace_id == rec.get("workspace_id") and not sub.ended:
                sub.queue.put_nowait(rec)

    def end_sub(self, sub: RunSub, reason: str) -> None:
        if sub.ended:
            return
        sub.ended = True
        self.subs.pop(sub.id, None)
        sub.session.subscriptions = max(0, sub.session.subscriptions - 1)
        if sub.task is not None and sub.task is not asyncio.current_task():
            sub.task.cancel()
        if not sub.session.closed:
            with contextlib.suppress(frames.FrameError):
                sub.session.send({"t": "evt", "sub": sub.id, "cursor": str(self.store.rev), "items": [], "end": reason})

    def end_all(self, reason: str) -> None:
        if self._watching is not None:
            self._watching.cancel()
        for task in self._checks.values():
            task.cancel()
        self._checks.clear()
        for sub in list(self.subs.values()):
            self.end_sub(sub, reason)

    def close_sub(self, session: Session, sid: str) -> bool:
        sub = self.subs.get(sid)
        if sub is None or sub.session is not session:
            return False
        asyncio.get_running_loop().call_soon(self.end_sub, sub, "closed")
        return True

    def open_sub(self, call: Call, seen: set[str], *, start: bool = True) -> RunSub:
        session = call.session
        if self.host.draining:
            raise self.host.restarting()
        if sum(1 for s in self.subs.values() if s.session is session) >= PER_CONNECTION:
            raise HostError("quota.host", f"at most {PER_CONNECTION} run subscriptions per connection")
        grant = (call.frame.get("auth") or {}).get("grant") if call.column.name == "G" else None
        sub = RunSub(
            "sub_" + secrets.token_hex(8), session, call.service.workspace_id, call.column.name, str(call.viewer),
            session.peer.uid, call.principal, grant,
            call.verified.grant.id if call.verified.grant is not None else None, seen,
        )  # fmt: skip
        sub.last_grant = self.grant_view(sub.workspace_id, sub.person)
        self.subs[sub.id] = sub
        session.subscriptions += 1
        session.on_close.append(lambda: self.end_sub(sub, "closed"))
        if start:
            self.start_sub(sub)
        return sub

    def start_sub(self, sub: RunSub) -> None:
        if not sub.ended and sub.task is None:
            sub.task = asyncio.get_running_loop().create_task(self._deliver(sub))

    async def _deliver(self, sub: RunSub) -> None:
        """One subscription's items, in order: revalidate the principal, then the run's visibility, then send."""
        try:
            while not sub.ended:
                rec = await sub.queue.get()
                service = self.host.services.get(sub.workspace_id)
                auth = getattr(service, "auth", None)
                if service is not None and auth is not None and not auth.fresh(service.entry.store_dir):
                    # as the bus: never deliver on a snapshot older than the log (F5); the worker refreshes it
                    await asyncio.wrap_future(service.submit(lambda s=service: (s.refresh(), s.sync(check=True))))
                    auth = getattr(service, "auth", None)
                if service is None or auth is None or auth.stamp is None or not auth.fresh(service.entry.store_dir):
                    self.end_sub(sub, "reset")
                    return
                reason = self.host.bus.principal_end(sub, auth, service.clock())  # type: ignore[arg-type]
                if reason is not None:
                    self.end_sub(sub, reason)
                    return
                if rec.get("_runner"):
                    now = self.grant_view(sub.workspace_id, sub.person)
                    if now == sub.last_grant:
                        continue
                    sub.last_grant = now
                    sub.grant_items += 1
                    item = {"id": f"{self.store.rev}.g{sub.grant_items}", "t": "runner", "grant": now}
                    cursor = str(self.store.rev)
                    if not await self._send_item(sub, item, cursor):
                        return
                    continue
                shown = not rec.get("_gone") and self.visible(rec, sub.column, sub.person)
                if shown and rec.get("ticket"):
                    shown = await asyncio.wrap_future(service.submit(_ticket_visible, service, sub, rec["ticket"]))
                if shown:
                    item = {"id": f"{rec['rev']}.r", "t": "run", "run": view(rec)}
                    sub.seen.add(rec["run"])
                elif rec["run"] in sub.seen:
                    sub.seen.discard(rec["run"])
                    item = {"id": f"{rec['rev']}.r", "t": "gone", "run": rec["run"]}
                else:
                    continue
                if not await self._send_item(sub, item, str(rec["rev"])):
                    return
        except asyncio.CancelledError:
            pass

    async def _send_item(self, sub: RunSub, item: dict[str, Any], cursor: str) -> bool:
        """Send one item; ``False`` when the subscription ended instead."""
        if sub.session.closed:
            self.end_sub(sub, "closed")
            return False
        try:
            sub.session.send({"t": "evt", "sub": sub.id, "cursor": cursor, "items": [item]})
        except frames.FrameError:
            self.end_sub(sub, "overflow")
            return False
        return True


class _Undecided(Exception):
    """The watcher cannot tell yet whether an attachment may continue (no fresh authentication snapshot)."""


def _take(buckets: dict[str, list[float]], key: str, rate: float, burst: float) -> bool:
    """A token bucket per key: ``rate`` per second, at most ``burst``; ``True`` when one was taken."""
    now = time.monotonic()
    level, at = buckets.get(key, (burst, now))
    level = min(burst, level + (now - at) * rate)
    if level < 1:
        buckets[key] = [level, now]
        return False
    buckets[key] = [level - 1, now]
    return True


def _retry_ms(buckets: dict[str, list[float]], key: str, rate: float) -> int:
    """The milliseconds until bucket ``key`` holds one token again (``retry_after_ms``, §0.6: 1 .. 3 600 000)."""
    level, at = buckets.get(key, (0.0, time.monotonic()))
    level += (time.monotonic() - at) * rate
    return max(1, min(3_600_000, math.ceil((1 - level) / rate * 1000)))


def _ticket_visible(service: Any, sub: RunSub, key: str) -> bool:
    """On the worker thread: may the person of ``sub`` (P, or G's grant person) see ticket ``key`` now?"""
    from .visibility import Viewer

    try:
        Viewer(sub.column, sub.person).ticket(service.store, key)
        return True
    except Exception:  # noqa: BLE001 - not visible, or not checkable: not shown
        return False


def _exit(reason: str, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"code": None, "signal": None, "at": _iso(), "reason": reason}
    if error:
        out["error"] = error
    return out


def _hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A ULID (48-bit ms time, 80 random bits), Crockford base32."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    out = ""
    for _ in range(26):
        out = _CROCKFORD[n & 31] + out
        n >>= 5
    return out


def new_run_id() -> str:
    return "r_" + new_ulid()


# -- the workspace ops (on the worker thread) ---------------------------------------------------------------------


#: seconds without terminal output before an agent run with nothing open counts as "may need you" (no hook marker)
QUIET_SECONDS = 90.0


def waiting_of(service: Any, hub: Any, rec: dict[str, Any]) -> dict[str, Any] | None:
    """``{since, source}`` when the agent of this run may be waiting for the person in its terminal (the Notification
    hook's marker, else the terminal quiet for :data:`QUIET_SECONDS`), else ``None``. Local to this machine and display
    only: it is never signed, logged or sent to a hub. New output clears it."""
    if hub is None or rec.get("kind") != "agent" or rec.get("state") not in LIVE or not rec.get("ticket"):
        return None
    seen = hub.activity.get(rec["run"])
    now = time.time()
    session = rec.get("session")
    layout = getattr(service.store, "layout", None)
    if session and layout is not None:
        from orch.instructions import marker_path, read_marker
        from orch.ops.runtime import records_dir

        try:
            mark = read_marker(marker_path(records_dir(layout), session).read_bytes(), now)
        except OSError:
            mark = None
        if mark is not None and (seen is None or seen[1] <= mark["at"]):
            return {"since": _iso(mark["at"]), "source": "hook"}
    quiet = float(os.environ.get("ORCH_TERMINAL_QUIET_SECONDS") or QUIET_SECONDS)
    if seen is not None and now - seen[1] >= quiet:
        return {"since": _iso(seen[1]), "source": "quiet"}
    return None


def serve(call: Call) -> dict[str, Any]:
    """``run.list`` and the worker half of ``run.sub`` (the visible runs now); the column was checked by the gate."""
    from .outcomes import request_idem
    from .visibility import Viewer
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    validate(op, args)
    request_idem(op, call.frame.get("opts"))
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    hub: RunnerHub | None = getattr(service, "runners", None)
    viewer = Viewer.of(call)
    column, person = call.column.name, str(call.viewer)
    wanted = None
    if args.get("ticket"):
        wanted = viewer.ticket(service.store, args["ticket"]).key
    records = [] if hub is None else list(hub.store.records.values())
    shown = []
    for rec in records:
        if rec.get("workspace_id") != service.workspace_id or hub is None or not hub.visible(rec, column, person):
            continue
        if rec.get("ticket"):  # P and G: a run on a ticket the caller's person cannot see is not shown (F6)
            try:
                viewer.ticket(service.store, rec["ticket"])
            except HostError:
                continue
        shown.append(rec)
    if op == "run.sub":
        return ok_result({"seen": [r["run"] for r in shown]})
    if args.get("state", "live") == "live":
        shown = [r for r in shown if r.get("state") in LIVE]
    if args.get("kind"):
        shown = [r for r in shown if r.get("kind") == args["kind"]]
    if wanted is not None:
        shown = [r for r in shown if r.get("ticket") == wanted]
    if args.get("since") is not None:  # only for the gap between run.sub and run.list (§12.3.2)
        shown = [r for r in shown if int(r.get("rev") or 0) > args["since"]]
    shown.sort(key=lambda r: -int(r.get("rev") or 0))
    offset = 0
    if args.get("page"):
        page = args["page"]
        if not re.fullmatch(r"o[0-9]{1,6}", page):
            raise HostError("page.expired", "run.list: start again without page")
        offset = int(page[1:])
    window = shown[offset : offset + 256]
    more = offset + 256 < len(shown)
    from .terms import runner_info

    runner = runner_info(service, person)
    runs = [view(r) | {"out_bytes": int(r.get("out") or 0), "waiting": waiting_of(service, hub, r)} for r in window]
    return ok_result({"runs": runs, "next_page": f"o{offset + 256}" if more else None, "runner": runner})
