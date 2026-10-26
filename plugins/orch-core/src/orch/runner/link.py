"""The runner's connection to the host (D-P2-24, D-P2-25, D-P2-39).

The runner is a client of the host's runner socket ``<run>/runner/host.sock``. Before it sends a byte it checks the
socket's peer UID against the host UID it was configured with (:func:`orch.host.client.connect_pinned`), so it never
takes commands or grants from a process of another UID; then the handshake, then ``runner.hello {version, gen, pid,
term_socket, runs, grants}`` (§12.3.3) with every run of its table (the host reconciles; it treats the report as
evidence only about runs it knows). The answer opens the command stream: ``evt`` items ``{cmd, cid, args}``, each
answered with ``runner.report``.

**Service generation.** Every runner start draws a new ``gen`` (32 hex digits), so the host can tell a reconnect of
the same runner from a restarted one (runs lost then are ``runner_restarted``); the term socket's hello carries it
too.

**Authorization epochs.** Losing the host ends every attachment and refuses new tickets
(:meth:`orch.runner.term.TermListener.host_lost`); only the next hello's answer re-opens them. Spawns stay
idempotent by run id across reconnects (:meth:`orch.runner.supervise.Supervisor.spawn`).

Every ``spawn`` is checked again here (defence in depth, :func:`spawn_spec`; the host computes it):

* **argv** is a registry form only: ``["$SHELL", "-l"]`` (a shell), ``["$SHELL", "-l", "-c", 'exec "$0" "$@"',
  "claude"|"codex", …]`` (a harness through the login shell, only while ``runner.json`` leaves ``login_shell`` on), or
  an absolute program that ``runner.json`` names in ``programs``. ``$SHELL`` is the runner's own ``SHELL``, which
  must be an executable POSIX shell (else ``harness.shell_unsupported``).
* **Environment**: the variables named in ``env_names`` must be on the runner's allow-list or in ``runner.json``'s
  ``pass_env`` and are copied from the runner's own environment; ``env_fixed`` may only hold host-chosen values
  (``ORCH_TICKET``, ``ORCH_WORKTREE``, ``ORCH_WORKTREES``, ``ORCH_HARNESS``, ``CLAUDE_CODE_SUBAGENT_MODEL``); ``TERM``
  and ``COLORTERM`` are fixed. Nothing else passes; the host never sends a secret in a spawn.
* **The grant** (H11c, D-P2-27): an agent (``kind:"agent"``) names its person's runner grant by id and its session;
  the secret comes only from this runner's in-memory :class:`~orch.runner.grants.GrantStore` (``grant.put``) and is
  put in that agent's environment as ``ORCH_GRANT``, next to ``ORCH_SESSION``. A shell never gets either.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orch.host import frames
from orch.host._socket import recv
from orch.host.client import Pin, handshake_pinned

from . import rules, transcript, transcript_children, transcript_sources
from .config import ConfigError, RunnerConfig
from .grants import GrantError, GrantStore
from .supervise import SpawnError, SpawnSpec, Supervisor
from .table import now_iso
from .term import TermListener, TicketError

__all__ = ["ENV_FIXED_NAMES", "ENV_FROM_RUNNER", "HostLink", "build_env", "new_gen", "spawn_spec"]

LOG = logging.getLogger(__name__)
#: ``peer.denied`` answers in a row after which the runner exits to be started afresh (its unit restarts it)
DENIED_LIMIT = 3

#: §2.4: what a run may get from the runner's own environment (values never reach the host)
ENV_FROM_RUNNER = frozenset(
    {
        "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SSH_AUTH_SOCK",
        "ORCH_STATE_DIR", "XDG_CONFIG_HOME", "ORCH_HOST_RUN_DIR",
    }
)  # fmt: skip
ENV_FIXED_NAMES = frozenset(
    {
        "ORCH_TICKET",
        "ORCH_WORKSPACE",
        "ORCH_WORKTREE",
        "ORCH_WORKTREES",
        "ORCH_WORKDIR",
        "ORCH_HARNESS",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    }
)
#: the only script a login shell runs (:data:`orch.host.launch.LOGIN_EXEC`) and the registry's programs
LOGIN_EXEC = 'exec "$0" "$@"'
REGISTRY_PROGRAMS = frozenset({"claude", "codex"})
REGISTRY_PROGRAMS_BY_HARNESS = {"claude-code": "claude", "codex": "codex"}
_SESSION = re.compile(r"s_[0-7][0-9A-HJKMNP-TV-Z]{25}(?:\.[1-9][0-9]{0,3}){0,3}")  # P2-3: a delegated child's .n
_PERSON = re.compile(r"p_[0-9a-f]{32}")
FIXED = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
VERSION = "orch-runner/1"


def _loaded_release() -> str:
    from .service import current_release

    return current_release()


def build_env(
    args: Mapping[str, Any], own: Mapping[str, str], pass_env: frozenset[str] | tuple[str, ...] = ()
) -> dict[str, str]:
    names = args.get("env_names") or []
    fixed = args.get("env_fixed") or {}
    if not isinstance(names, list) or not isinstance(fixed, dict):
        raise SpawnError("invalid.input", "spawn: invalid environment")
    passing = ENV_FROM_RUNNER | frozenset(pass_env)
    bad = [n for n in names if n not in passing] + [k for k in fixed if k not in ENV_FIXED_NAMES]
    if bad:
        raise SpawnError("invalid.input", f"spawn: environment name not allowed: {str(bad[0])[:40]}")
    env = {n: own[n] for n in names if n in own}
    for k, v in fixed.items():
        if not isinstance(v, str) or "\0" in v:
            raise SpawnError("invalid.input", "spawn: invalid environment value")
        env[k] = v
    env.update(FIXED)
    return env


#: POSIX-compatible login shells (§12.4.1: a person's $SHELL must be one, else ``harness.shell_unsupported``)
POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash"})


def resolve_shell(argv: list[Any], own: Mapping[str, str]) -> list[Any]:
    """``["$SHELL", ...]`` becomes the runner's own ``SHELL`` (an absolute path to a POSIX shell, never anything the
    host sent); any other argv passes through (its own checks follow in :meth:`SpawnSpec.check`)."""
    if not argv or argv[0] != "$SHELL":
        return argv
    shell = own.get("SHELL") or ""
    if not shell.startswith("/") or os.path.basename(shell) not in POSIX_SHELLS or not os.access(shell, os.X_OK):
        raise SpawnError("harness.shell_unsupported", "the runner's $SHELL is not an executable POSIX shell")
    return [shell, *argv[1:]]


def _model_ok(harness: str | None, rest: list[str]) -> bool:
    """A ``--model`` in a registry launch comes first and names a model of that harness's closed list."""
    from orch.modelchoices import MODEL_CHOICES

    if "--model" not in rest:
        return True
    return harness in MODEL_CHOICES and rest[0] == "--model" and len(rest) >= 2 and rest[1] in MODEL_CHOICES[harness]


def registry_argv(argv: Any, own: Mapping[str, str], config: RunnerConfig) -> list[str]:
    """The argv of a registry launch with ``$SHELL`` resolved; anything else is ``invalid.input`` (D-P2-28: the client
    chooses values, never a command, and the runner checks that again)."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise SpawnError("invalid.input", "spawn: invalid argv")
    if argv[0] == "$SHELL":
        if argv[1:] == ["-l"]:
            return resolve_shell(argv, own)
        if len(argv) >= 5 and argv[1:4] == ["-l", "-c", LOGIN_EXEC] and argv[4] in REGISTRY_PROGRAMS:
            if not config.login_shell:
                raise SpawnError("invalid.input", "spawn: runner.json turns the login shell off")
            harness = next((h for h, prog in REGISTRY_PROGRAMS_BY_HARNESS.items() if prog == argv[4]), None)
            if not _model_ok(harness, argv[5:]):
                raise SpawnError("invalid.input", "spawn: --model names a model orch does not offer")
            return resolve_shell(argv, own)
        raise SpawnError("invalid.input", "spawn: argv is not a registry launch")
    if argv[0].startswith("/") and argv[0] in config.programs.values():
        harness = next((h for h, prog in config.programs.items() if prog == argv[0]), None)
        if not _model_ok(harness, argv[1:]):
            raise SpawnError("invalid.input", "spawn: --model names a model orch does not offer")
        return list(argv)
    raise SpawnError("invalid.input", "spawn: argv[0] is neither $SHELL nor a program runner.json names")


def _is_claude(argv: list[str]) -> bool:
    """A registry launch of Claude Code: ``... -c 'exec "$0" "$@"' claude ...`` or an absolute ``claude``."""
    prog = argv[4] if len(argv) >= 5 and argv[1:4] == ["-l", "-c", LOGIN_EXEC] else argv[0]
    return os.path.basename(prog) == "claude"


def _harness_of(argv: list[str]) -> str | None:
    """The registry harness a spawn argv starts (``claude-code``, ``codex``), ``None`` for a shell."""
    prog = argv[4] if len(argv) >= 5 and argv[1:4] == ["-l", "-c", LOGIN_EXEC] else argv[0]
    return {"claude": "claude-code", "codex": "codex"}.get(os.path.basename(prog))


def spawn_spec(args: Mapping[str, Any], own: Mapping[str, str], config: RunnerConfig, grants: GrantStore) -> SpawnSpec:
    """A ``spawn`` command as the :class:`SpawnSpec` the supervisor starts: the registry argv, the environment
    (allow-list, ``pass_env``, host-chosen fixed values) and, for an agent, ``ORCH_GRANT`` from the grant store and
    ``ORCH_SESSION``. Raises :class:`SpawnError`."""
    kind = args.get("kind")
    trusted = None
    argv = registry_argv(args.get("argv"), own, config)
    env = build_env(args, own, config.pass_env)
    grant, session = args.get("grant"), args.get("session")
    if kind == "agent":
        person, wid = args.get("person"), str(args.get("workspace_id"))
        if not (isinstance(session, str) and _SESSION.fullmatch(session)):
            raise SpawnError("invalid.input", "spawn: an agent needs a session id")
        if not (isinstance(person, str) and _PERSON.fullmatch(person)):
            raise SpawnError("invalid.input", "spawn: an agent needs its person")
        if grant is None:
            raise SpawnError("launch.no_grant", "spawn: an agent needs its person's runner grant")
        try:
            env["ORCH_GRANT"] = grants.secret_for(wid, person, grant)
        except GrantError as exc:
            raise SpawnError(exc.code, exc.message) from None
        env["ORCH_SESSION"] = session
        # run 6, #1066: no first-run trust prompt in a ticket work folder, and only for a launch that carries the
        # settings-isolation flags (a worktree, repo or root launch keeps Claude Code's own prompts)
        if _is_claude(argv) and "--setting-sources" in argv and "--strict-mcp-config" in argv:
            from .claude_trust import pretrust

            pin = args.get("cwd") or {}
            if pretrust(own, env.get("ORCH_WORKSPACE"), pin.get("path"), pin):
                trusted = (str(env.get("ORCH_WORKSPACE")), str(pin.get("path")))
    elif grant is not None or session is not None:
        raise SpawnError("invalid.input", "spawn: a shell acts as the person: no grant, no session")
    return SpawnSpec(
        str(args.get("run")), str(args.get("workspace_id")), str(kind), argv, env, dict(args.get("cwd") or {}),
        int(args.get("cols") or 120), int(args.get("rows") or 40), trusted,
    )  # fmt: skip


def new_gen() -> str:
    """The runner generation (§12.3.2 ``Gen``): 32 hex digits drawn at every runner start."""
    return secrets.token_hex(16)


class HostLink:
    """One thread that keeps the runner connected to its host and serves the command stream."""

    def __init__(
        self,
        sup: Supervisor,
        socket_path: Path,
        *,
        host_uid: int,
        gen: str,
        term: TermListener | None = None,
        env: Mapping[str, str] | None = None,
        backoff: tuple[float, float] = (0.5, 10.0),
        config: RunnerConfig | Callable[[], RunnerConfig] | None = None,
        grants: GrantStore | None = None,
        release: str | None = None,
        restart: Callable[[bool], dict[str, Any]] | None = None,
        restarting: Callable[[], bool] | None = None,
        on_denied: Callable[[], None] | None = None,
    ) -> None:
        self.sup = sup
        #: called once after :data:`DENIED_LIMIT` ``peer.denied`` refusals in a row (a stale view of who the runner
        #: is): the runner exits non-zero and its unit starts it afresh
        self._on_denied = on_denied
        self._denied = 0
        #: ``runner.restart`` (tier B, spec 13.2 item 7): arms the runner's own restart (:mod:`orch.runner.restart`)
        self._restart = restart
        #: true once a restart is armed: new spawns are refused, so none is accepted and then killed by the exit
        self._restarting = restarting or (lambda: False)
        #: the release this process LOADED, taken now (never on the first hello): the host refuses spawns from a
        #: runner of another release than its own (``runner.stale``)
        self.release = release if release is not None else _loaded_release()
        self.socket_path = Path(socket_path)
        self.host_uid = host_uid
        self.gen = gen
        self.term = term
        self.env = dict(os.environ if env is None else env)
        self.backoff = backoff
        #: ``runner.json`` (read again at every spawn when a loader is given: an edit applies to the next launch)
        self._config = config if config is not None else RunnerConfig()
        #: the runner grants, in memory only (D-P2-27)
        self.grants = grants if grants is not None else GrantStore()
        self.sock: Any = None
        self.epoch: str | None = None
        self.ready = threading.Event()
        self._write = threading.Lock()
        self._n = 0
        #: the connection number: bumped at every (re)connect, before the hello snapshot; every command is tagged
        #: with the number it arrived on and never runs under another (F1: a stale spawn never runs after a reconnect)
        self._conn = 0
        self._held: list[tuple[dict[str, Any], int]] = []  # commands that arrived before the hello's answer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="orch-runner-cmd")
        self._answers: dict[str, dict[str, Any]] = {}
        self._waiting: dict[str, threading.Event] = {}
        self._repo_locks: dict[str, threading.Lock] = {}
        #: run -> (workspace root, work folder) a Claude Code trust entry was recorded for
        self._trusted: dict[str, tuple[str, str]] = {}
        #: tests: drop the report of these commands (a lost ack)
        self.drop_reports: set[str] = set()
        sup.on_event = self._on_event
        if term is not None:
            term.on_change = self._term_changed
            term.on_activity = self._term_activity

    # -- life cycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="orch-runner-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def config(self) -> RunnerConfig:
        """``runner.json`` now; an invalid file refuses the launch (``launch.invalid``), never defaults."""
        if isinstance(self._config, RunnerConfig):
            return self._config
        try:
            return self._config()
        except ConfigError as exc:
            raise SpawnError("launch.invalid", str(exc)[:300]) from None

    def disconnect(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(2)
            sock.close()

    def _loop(self) -> None:
        delay = self.backoff[0]
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:  # noqa: BLE001 - any failure: reconnect later
                LOG.info("runner link: %s", exc)
                self._count_denial(exc)
            finally:
                if self.ready.is_set():
                    self._denied = 0
                    delay = self.backoff[0]  # it was connected: the next attempt starts from the short delay
                self.ready.clear()
                self.epoch = None
                with self.sup._spawn_lock:
                    self._conn += 1  # every command of the lost connection is stale from here on
                    self._held.clear()
                if self.term is not None:
                    self.term.host_lost("host.restarting")
                self.disconnect()
            self._stop.wait(delay)
            delay = min(delay * 2, self.backoff[1])

    def _count_denial(self, exc: Exception) -> None:
        if getattr(exc, "code", None) != "peer.denied":
            self._denied = 0
            return
        self._denied += 1
        if self._denied >= DENIED_LIMIT and self._on_denied is not None:
            LOG.error("the host refused this runner %d times in a row: exiting for a fresh start", self._denied)
            self._on_denied()

    def _session(self) -> None:
        hello = {"t": "hello", "proto": [1], "client": "orch-runner", "caps": ["runner/1"], "ops": {}}
        sock, welcome = handshake_pinned(Pin(self.host_uid, None, self.socket_path), hello)  # peer UID first
        if (welcome.get("peer") or {}).get("class") != "runner":
            sock.close()
            raise ConnectionError("the host did not accept this process as the runner")
        sock.settimeout(None)
        self.sock = sock
        decoder = frames.Decoder()
        term_socket = str(self.term.path) if self.term is not None else ""
        with self.sup._spawn_lock:  # no spawn half-done while the table is reported
            self._conn += 1
            conn = self._conn
            runs = self._runs()
        reported_running = {r["run"] for r in runs if r["state"] == "running"}
        hello_args = {"version": VERSION, "release": self.release, "gen": self.gen, "pid": os.getpid(),
                      "term_socket": term_socket, "runs": runs, "grants": self.grants.report()}  # fmt: skip
        rid = self._send_req("runner.hello", hello_args)
        while not self._stop.is_set():
            data = recv(sock)
            if not data:
                raise ConnectionError("the host closed the connection")
            for frame in decoder.feed(data):
                if isinstance(frame, frames.FrameError):
                    raise frame
                kind = frame.get("t")
                if kind == "res":
                    if frame.get("id") == rid:
                        if not frame.get("ok"):
                            raise ConnectionError(f"runner.hello refused: {frame.get('error')}")
                        data_ = frame["result"]["data"]
                        self.epoch = str(data_.get("epoch"))
                        if self.term is not None:
                            self.term.host_ready(conn)
                        with self.sup._spawn_lock:
                            self.ready.set()
                            held, self._held = self._held, []
                        for item, tag in held:
                            self._dispatch(item, tag)
                        self._resend_exits(reported_running)
                    else:
                        event = self._waiting.pop(frame.get("id"), None)
                        if event is not None:  # only answers someone waits for are kept
                            self._answers[frame.get("id")] = frame
                            event.set()
                elif kind == "evt":
                    for item in frame.get("items") or []:
                        if "op" in item and "cmd" in item:
                            with self.sup._spawn_lock:
                                if not self.ready.is_set():  # held until the hello's answer
                                    self._held.append((item, conn))
                                    continue
                            self._dispatch(item, conn)
                elif kind == "fatal":
                    raise ConnectionError(f"the host closed the connection: {frame.get('error')}")

    def _runs(self) -> list[dict[str, Any]]:
        """§12.3.3 ``runs``: every running run first, then the newest ended ones, at most 256 in all; a half-started
        spawn reads as lost."""
        out = []
        every = sorted(self.sup.runs(), key=lambda x: str(x.get("started_at") or ""), reverse=True)
        running = [r for r in every if r.get("state") == "running"]
        ended = [r for r in every if r.get("state") != "running"]
        for r in (running + ended)[:256]:
            state = r.get("state") if r.get("state") in ("running", "exited", "lost") else "lost"
            exit_ = r.get("exit")
            if isinstance(exit_, dict):
                exit_ = {k: exit_.get(k) for k in ("code", "signal", "at")} | {
                    "reason": exit_.get("reason"), "cleanup": r.get("cleanup"),
                }  # fmt: skip
                if exit_["reason"] not in ("exited", "stopped", "spawn_failed", "lost", "runner_restarted"):
                    del exit_["reason"]
            rec = self.sup.table.get(r["run"]) or {}
            out.append(
                {
                    "run": r["run"],
                    "workspace_id": r.get("workspace_id") or "0" * 32,
                    "pid": r.get("pid"),
                    "birth": r.get("birth"),
                    "pgid": r.get("pgid"),
                    "sid": r.get("sid"),
                    "cgroup": rec.get("cgroup"),
                    "started_at": r.get("started_at"),
                    "state": state,
                    "out": int(r.get("out") or rec.get("out") or 0),
                    "exit": exit_,
                }  # fmt: skip
            )
        return out

    # -- frames
    def _send_req(self, op: str, args: dict[str, Any], event: threading.Event | None = None) -> str:
        sock = self.sock
        if sock is None:
            raise ConnectionError("not connected")
        with self._write:
            self._n += 1
            rid = f"r{self._n}"
            if event is not None:
                self._waiting[rid] = event
            sock.sendall(frames.encode({"t": "req", "id": rid, "op": op, "args": args}))
        return rid

    def call(self, op: str, args: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        """A request whose answer the caller needs (tests; the runner itself only fires reports)."""
        event = threading.Event()
        rid = self._send_req(op, args, event)
        if not event.wait(timeout):
            self._waiting.pop(rid, None)
            raise TimeoutError(op)
        return self._answers.pop(rid)

    def _report(self, cmd: str, outcome: str, payload: dict[str, Any]) -> None:
        args: dict[str, Any] = {"cmd": cmd, "outcome": outcome}
        args["result" if outcome == "ok" else "error"] = payload
        try:
            self._send_req("runner.report", args)
        except (OSError, ConnectionError):
            pass  # the host reconciles at the next hello

    def _resend_exits(self, reported_running: set[str]) -> None:
        """Runs reported running in the hello that ended before its answer: their exit goes out now."""
        for run in reported_running:
            rec = self.sup.table.get(run) or {}
            if rec.get("state") != "running" and rec.get("cleanup"):
                self._on_event("cleanup", run, {"cleanup": rec["cleanup"]})

    # -- commands
    def _repo_lock(self, repo: str) -> threading.Lock:
        with self._write:
            return self._repo_locks.setdefault(repo, threading.Lock())

    def _current(self, tag: int) -> bool:
        return tag == self._conn and self.ready.is_set()

    def _dispatch(self, item: dict[str, Any], tag: int) -> None:
        """``attach.end`` runs inline on this (the link) thread: it never blocks (the term socket's ``end`` takes its
        lock, queues and reports), and it must not wait behind ``stop``/``spawn`` in the command pool, or the host's
        5 s confirmation deadline would drop the whole runner session. Every other command goes to the pool."""
        if item.get("op") in ("attach.end", "grant.put", "grant.drop"):
            # grant.put and grant.drop run inline too, in arrival order (#466 review round 2): the host sends a forget's
            # drop after the put it follows, so the drop can never overtake that put in the pool (a client that went
            # away or a host timeout releases the host's per-person lock while the put is still in flight)
            self._command(item, tag)
        else:
            self._pool.submit(self._command, item, tag)

    def _command(self, item: dict[str, Any], tag: int) -> None:
        cmd, cid, args = item.get("op"), str(item.get("cmd")), item.get("args") or {}
        if not self._current(tag):
            LOG.info("dropped a %s command of a lost host connection", str(cmd)[:32])
            return  # never runs, never reported: the host already reconciled it at the new hello
        try:
            result = self._run(str(cmd), args, tag)
        except _Stale:
            return
        except SpawnError as exc:
            outcome, payload = "error", {"code": exc.code, "message": exc.message}
        except TicketError as exc:
            outcome, payload = "error", {"code": exc.code, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never a traceback to the host
            LOG.exception("runner command %s failed", cmd)
            outcome, payload = "error", {"code": "run.spawn_failed", "message": type(exc).__name__}
        else:
            outcome, payload = "ok", result
        if cmd in self.drop_reports:
            return
        self._report(cid, outcome, payload)

    def _run(self, cmd: str, args: dict[str, Any], tag: int) -> dict[str, Any]:
        if cmd == "spawn":
            spec = spawn_spec(args, self.env, self.config(), self.grants)
            spec.check()
            spec = dataclasses.replace(spec, argv=rules.prepare(self._rules_base(), spec.run, spec.argv, spec.env))
            spec = dataclasses.replace(spec, argv=transcript.prepare(spec.run, spec.argv))
            if spec.trusted is not None:
                self._trusted[spec.run] = spec.trusted
            with self.sup._spawn_lock:  # the epoch check and the spawn are one step against a reconnect's snapshot
                if not self._current(tag):
                    raise _Stale()
                if self._restarting():
                    raise SpawnError("runner.unavailable", "the agent runner is restarting on a new release; try again")
                res = self.sup.spawn(spec)
                if not res.get("duplicate"):  # the transcript reader finds the run's file from these
                    self.sup.table.update(spec.run, harness=_harness_of(spec.argv), cwd_path=spec.cwd.get("path"))
            return {k: res.get(k) for k in ("pid", "state", "started_at", "duplicate")}
        if cmd == "stop":
            run = str(args.get("run"))
            if self.term is not None:  # the person stopped it: its viewers end with run.stopped (§12.6.2)
                self.term.end_run(run, "run.stopped")
            return self.sup.stop(run, grace=max(0, int(args.get("grace_ms", 10_000))) / 1000)
        if cmd == "attach.allow":
            if self.term is None:
                raise TicketError("no terminal socket", "runner.unavailable")
            return self.term.allow(args, epoch=tag)  # the epoch it arrived in, not the one current when it runs (F4)
        if cmd == "git":  # H11d: worktree git, one command per repo at a time, 30 s each (§12.7)
            from .wtgit import run_git

            pin = args.get("repo_pin") if isinstance(args.get("repo_pin"), dict) else {}
            with self._repo_lock(str(pin.get("path"))):
                if not self._current(tag):
                    raise _Stale()
                return run_git(args, self.env, log=LOG.info)
        if cmd == "transcript":  # focus phase 2: the run's harness transcript as chat items
            if args.get("sources"):  # P2-4: what the run read (web pages, searches, files)
                return transcript_sources.serve(args, self.sup, self.env)
            if args.get("children"):  # P2-2: the delegated children of the run
                return transcript_children.serve(args, self.sup, self.env)
            return transcript.serve(args, self.sup, self.env)
        if cmd == "grant.put":  # H11c: the secret arrives here once; it is answered without it
            try:
                return self.grants.put(args)
            except GrantError as exc:
                raise SpawnError(exc.code, exc.message) from None
        if cmd == "grant.drop":
            return {"dropped": self.grants.drop(args.get("grant"))}
        if cmd == "restart":
            if self._restart is None:
                raise SpawnError("runner.unavailable", "this runner cannot restart itself")
            return self._restart(args.get("force") is True)
        if cmd == "attach.end":
            if self.term is not None:
                self.term.end(str(args.get("att")), str(args.get("reason") or "detached"))
            return {}
        raise SpawnError("invalid.input", f"unknown command {cmd[:32]}")

    # -- the term socket's reports
    def _term_changed(self, run: str) -> None:
        """A run's attachments, lease or size changed: ``runner.term`` (the host's counts and ``Run.viewers``)."""
        if not self.ready.is_set() or self.term is None:
            return
        args = self.term.summary(run)
        if args["cols"] is None:
            args.pop("cols")
            args.pop("rows")
        with contextlib.suppress(OSError, ConnectionError):
            self._send_req("runner.term", args)

    def _term_activity(self, run: str) -> None:
        """The run printed: ``runner.term`` again with ``out``, so the host knows the terminal is not quiet."""
        if not self.ready.is_set() or self.term is None:
            return
        ring = self.sup.ring(run)
        args = self.term.summary(run)
        if args["cols"] is None:
            args.pop("cols")
            args.pop("rows")
        if ring is not None:
            args["out"] = ring.end
        with contextlib.suppress(OSError, ConnectionError):
            self._send_req("runner.term", args)

    # -- supervisor events
    def _rules_base(self) -> Path:
        return Path(self.sup.table.directory)

    def _after_exit(self, run: str) -> None:
        """The run is over: its rules file goes, and its Claude Code trust is checked again (somebody may have put
        settings or a tracked file in the folder meanwhile)."""
        rules.remove(self._rules_base(), run)
        got = self._trusted.pop(run, None)
        if got is not None:
            from .claude_trust import refresh

            refresh(self.env, *got)

    def _on_event(self, kind: str, run: str, payload: dict[str, Any]) -> None:
        if kind == "exit":
            with contextlib.suppress(Exception):
                self._after_exit(run)
        if not self.ready.is_set() or kind != "cleanup":
            return  # the next hello carries the state; runner.exit is sent once the clean-up has a result
        rec = self.sup.table.get(run) or {}
        exit_ = rec.get("exit") or {}
        if not exit_:
            return  # a leader still alive (incomplete stop): no exit to report yet
        args: dict[str, Any] = {
            "run": run,
            "code": exit_.get("code"),
            "signal": exit_.get("signal"),
            "at": exit_.get("at") or now_iso(),
            "cleanup": payload.get("cleanup") or "unknown",
        }
        with contextlib.suppress(OSError, ConnectionError):
            self._send_req("runner.exit", args)


class _Stale(Exception):
    """A command of a lost host connection."""


def wait_ready(link: HostLink, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if link.ready.wait(0.05):
            return True
    return False
