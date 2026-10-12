"""The host's side of the runner (H11a, D-P2-24, D-P2-25, D-P2-37, D-P2-39): the runner socket, the command stream,
the host's run records and the run lifecycle reads.

**The runner socket** ``<run>/runner/host.sock`` (0700 directory, host-owned) accepts peer class ``runner`` only from
the runner unit's service-manager main PID (:func:`runner_identity`: ``launchctl print gui/<uid>/io.orch.runner``,
``systemctl --user show -p MainPID orch-runner.service``), and one connection per runner UID (a second one is
``peer.denied``). Tier A: the runner UID is the host UID, so this is fault separation and hygiene, not a boundary:
same-UID separation is not containment; runner reports are untrusted and cannot establish authorization; reports are
accepted only for runs assigned to that runner (its UID and service generation). The runner checks the host too: its
connect refuses a socket whose peer UID is not the host UID (:func:`orch.host.client.connect_pinned`).

**The protocol** (cap ``runner/1``). After ``welcome`` the runner sends ``runner.hello {version, generation, runs}``;
the answer ``{sub, epoch}`` opens the host's command stream: ``evt`` items ``{id:"<n>.c", cmd, cid, args}``. The runner
answers each with ``runner.report {cid, outcome, result|error}`` and reports exits with ``runner.exit``. Commands:
``spawn``, ``stop``, ``attach.allow``, ``attach.end``. Every hello is a new **authorization epoch**: the runner ends all
attachments and accepts no new ticket while it is disconnected, and only the hello's answer re-opens it.

**Run records** (D-P2-37): ``<state>/hosts/<ws>/runs/<run>.json`` (0600 in 0700). The launch intent (run id, its
fields, the runner UID and generation) is written **before** the ``spawn`` command is sent; the runner dedupes
``spawn`` by run id, so a lost ack never starts a second process. ``runner.hello`` reconciles: a live record the runner
does not report becomes ``lost`` (never spawned again); a run the runner reports as lost after a generation change is
``runner_restarted``; a run the host does not know is ignored (hello is evidence only about runs the host knows). Each
change takes the next host-wide ``rev``.

**Reads** (cap ``run/1``): ``run.list`` and ``run.sub`` for columns P and G (N: ``op.not_allowed``). P sees the
caller person's runs; G sees only ``kind:"agent"`` runs of its grant's person on tickets the grant can see. A client
subscribes first (``run.sub`` answers the current ``rev``), then lists, and keeps the higher ``rev`` per run. Items:
``{id:"<rev>.r", t:"run", run}`` after every change and ``{t:"gone", run}`` when a run leaves the caller's view.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import frames
from .custody import ensure_private_dir, read_json, write_private
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
]

LOG = logging.getLogger(__name__)

RUNNER_OPS: dict[str, int] = {"runner.hello": 1, "runner.report": 1, "runner.exit": 1}
RUNNER_CAPS = ["runner/1"]
RUN_OPS: dict[str, int] = {"run.list": 1, "run.sub": 1}
RUN_CAPS = ["run/1"]
COMMAND_TIMEOUT = 15.0
ATTACH_SECONDS = 30
LIVE = ("starting", "running")
RUN_ID = r"^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
#: codes a runner may report for a command; anything else becomes ``run.spawn_failed`` / ``internal``
RUNNER_CODES = frozenset(
    {"launch.cwd_changed", "harness.not_found", "run.spawn_failed", "invalid.input", "not_found", "term.busy"}
)
KEEP_SECONDS = 7 * 24 * 3600
KEEP_ENDED = 1000
PER_CONNECTION = 8


def _iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


# -- the main-PID check ------------------------------------------------------------------------------------------------


def main_pid(uid: int, run: Callable[[list[str]], Any] | None = None, *, platform: str | None = None) -> int | None:
    """The runner unit's main PID from the person's service manager, or ``None``. ``run`` defaults to the H7 command
    runner, which refuses under pytest and with ``ORCH_NO_SERVICE_MANAGER=1`` (so a test never reaches launchctl)."""
    from orch.runner.service import LAUNCHD_LABEL, SYSTEMD_NAME

    from .enable import _default_run

    run = run or _default_run
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            res = run(["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"])
            found = re.search(r"^\s*pid = ([0-9]+)\s*$", res.stdout or "", re.M) if res.returncode == 0 else None
        elif platform.startswith("linux"):
            res = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", SYSTEMD_NAME])
            found = re.fullmatch(r"\s*([0-9]+)\s*", res.stdout or "") if res.returncode == 0 else None
        else:
            return None
    except OSError:
        return None
    pid = int(found.group(1)) if found else 0
    return pid if pid > 1 else None


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
_EXIT = _obj(
    {
        "code": {"type": ["integer", "null"]},
        "signal": {"type": ["string", "null"], "maxLength": 16},
        "at": _TIME,
        "reason": {"enum": ["exited", "stopped", "spawn_failed", "lost", "runner_restarted"]},
        "error": {"type": "string", "maxLength": 64},
    },
    ("code", "signal", "at", "reason"),
)
_CLEANUP = {"enum": ["complete", "incomplete", "unknown", None]}
_REPORTED = _obj(
    {
        "run": _RUN,
        "workspace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "kind": {"enum": ["agent", "shell", None]},
        "state": {"enum": ["spawning", "running", "exited", "lost"]},
        "pid": {"type": ["integer", "null"]},
        "started_at": {"type": ["string", "null"], "maxLength": 40},
        "exit": {"anyOf": [_EXIT, {"type": "null"}]},
        "cleanup": _CLEANUP,
    },
    ("run", "state"),
)
RUNNER_SCHEMAS: dict[str, dict[str, Any]] = {
    "runner.hello": _obj(
        {
            "version": {"type": "string", "minLength": 1, "maxLength": 64},
            "generation": {"type": "integer", "minimum": 1},
            "runs": {"type": "array", "maxItems": 2048, "items": _REPORTED},
        },
        ("version", "generation", "runs"),
    ),
    "runner.report": _obj(
        {
            "cid": {"type": "string", "pattern": "^c_[0-9a-f]{16}$"},
            "outcome": {"enum": ["ok", "error"]},
            "result": {"type": "object"},
            "error": _obj(
                {"code": {"type": "string", "minLength": 1, "maxLength": 64}, "message": {"type": "string"}},
                ("code",),
            ),
        },
        ("cid", "outcome"),
    ),
    "runner.exit": _obj({"run": _RUN, "exit": _EXIT, "cleanup": _CLEANUP}, ("run",)),
}
RUN_SCHEMAS: dict[str, dict[str, Any]] = {
    "run.list": _obj(
        {
            "state": {"enum": ["live", "all"]},
            "kind": {"enum": ["agent", "shell"]},
            "ticket": {"type": "string", "minLength": 1, "maxLength": 64},
        }
    ),
    "run.sub": _obj({}),
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in {**RUNNER_SCHEMAS, **RUN_SCHEMAS}.items()}


def validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


# -- the records -------------------------------------------------------------------------------------------------------

#: the fields of a record a client sees (``Run``); pid, runner UID, generation and the spawn payload stay host-side
VIEW_FIELDS = (
    "run", "kind", "person", "ticket", "harness", "mode", "model", "session", "label", "state", "started_at", "exit",
    "cleanup", "rev",
)  # fmt: skip


def view(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec.get(k) for k in VIEW_FIELDS}


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
        self.generation: int | None = None
        self.epoch = 0
        self.instance = secrets.token_hex(4)
        self.sub_id: str | None = None
        self.seq = 0
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.subs: dict[str, RunSub] = {}
        self.attachments: dict[str, dict[str, Any]] = {}
        self._save_lock = asyncio.Lock()
        self.ignored = 0

    # -- state
    @property
    def connected(self) -> bool:
        return self.session is not None and not self.session.closed

    @property
    def term_socket(self) -> Path:
        return (
            Path(self.host.run_dir) / f"term-{self.uid if self.uid is not None else self.host.host_uid}" / "term.sock"
        )

    def load(self) -> None:
        self.store.load()

    async def _save(self, rec: dict[str, Any], *, publish: bool = True) -> dict[str, Any]:
        async with self._save_lock:
            self.store.rev += 1
            rec["rev"] = self.store.rev
            if rec.get("state") not in LIVE and "ended_ts" not in rec:
                rec["ended_ts"] = time.time()
            self.store.records[rec["run"]] = rec
            await asyncio.to_thread(self.store.save, dict(rec))
        if publish:
            self.publish(rec)
        return rec

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
                result = await (self.report(args) if op == "runner.report" else self.exit(session, args))
            from .workspace import ok_result

            session.respond(request, {"t": "res", "id": request.id, "ok": True, "result": ok_result(result)})
            if op == "runner.hello":
                self._after_hello()
        except HostError as exc:
            session.respond(request, exc.response(request.id))

    # -- runner ops
    async def hello(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        if self.session is not None and self.session is not session and not self.session.closed:
            raise HostError("peer.denied", "another runner connection is live")
        if self.session is session:
            raise HostError("req.invalid", "runner.hello was already sent on this connection")
        previous = self.generation
        self.session, self.uid, self.generation = session, session.peer.uid, int(args["generation"])
        self.epoch += 1
        self.sub_id = "sub_" + secrets.token_hex(8)
        session.subscriptions += 1  # the command stream keeps the connection out of the idle close
        session.on_close.append(lambda: self.disconnected(session))
        reported = {r["run"]: r for r in args["runs"]}
        reconciled = 0
        for run, rec in list(self.store.records.items()):
            if rec.get("runner_uid") != self.uid:
                continue
            rep = reported.get(run)
            if rec.get("state") in LIVE:
                await self._apply(rec, rep, previous_generation=previous)
                reconciled += 1
            elif rep is not None and rec.get("cleanup") is None and rep.get("cleanup"):
                await self._save({**rec, "cleanup": rep["cleanup"]})
        self.ignored += sum(1 for run in reported if run not in self.store.records)
        LOG.info("runner connected: uid %s generation %s epoch %s", self.uid, self.generation, self.epoch)
        return {"sub": self.sub_id, "epoch": f"{self.instance}.{self.epoch}", "reconciled": reconciled}

    def _after_hello(self) -> None:
        """Nothing is queued yet; H11c re-sends grant state here."""

    async def _apply(self, rec: dict[str, Any], rep: dict[str, Any] | None, *, previous_generation: int | None) -> None:
        """Reconcile one live record with the runner's report of it (``None``: not reported)."""
        if rep is None:  # never spawned again: the runner has no record of it (D-P2-37)
            await self._save({**rec, "state": "lost", "cleanup": "unknown", "exit": _exit("lost")})
            return
        state = rep["state"]
        new = dict(rec)
        if state in ("spawning", "running"):
            if state == "running":
                new.update(
                    state="running", pid=rep.get("pid"), started_at=rep.get("started_at") or rec.get("started_at")
                )
            if new != rec:
                await self._save(new)
            return
        exit_ = rep.get("exit") or _exit("lost")
        if state == "lost":
            reason = "runner_restarted" if rec.get("generation") != self.generation else "lost"
            exit_ = {**exit_, "reason": reason}
            new.update(state="lost", exit=exit_, cleanup=rep.get("cleanup") or "unknown")
        else:
            new.update(state="exited", exit=exit_, cleanup=rep.get("cleanup"))
        await self._save(new)

    async def report(self, args: dict[str, Any]) -> dict[str, Any]:
        future = self.pending.pop(args["cid"], None)
        if future is None or future.done():
            raise HostError("not_found", "no such command")
        if args["outcome"] == "ok":
            future.set_result(args.get("result") or {})
        else:
            err = args.get("error") or {}
            code = err.get("code") if err.get("code") in RUNNER_CODES else "run.spawn_failed"
            future.set_exception(HostError(code, str(err.get("message") or code)[:400]))
        return {}

    async def exit(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        rec = self.store.records.get(args["run"])
        if rec is None or rec.get("runner_uid") != session.peer.uid or rec.get("generation") != self.generation:
            raise HostError("not_found", "no such run on this runner")
        new = dict(rec)
        if "exit" in args and rec.get("state") in LIVE:
            new.update(state="exited", exit=args["exit"])
        if args.get("cleanup") and rec.get("cleanup") is None:
            new["cleanup"] = args["cleanup"]
        if new != rec:
            await self._save(new)
        return {}

    def disconnected(self, session: Session) -> None:
        if self.session is not session:
            return
        self.session = None
        self.sub_id = None
        for future in self.pending.values():
            if not future.done():
                future.set_exception(HostError("outcome.unknown", "the runner disconnected before it answered"))
        self.pending.clear()
        self.attachments.clear()
        LOG.warning("runner disconnected (uid %s)", self.uid)

    # -- commands
    async def command(self, cmd: str, args: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        session = self.session
        if session is None or session.closed or self.sub_id is None:
            raise HostError("runner.unavailable", "no runner is connected", retry_after_ms=1000)
        cid = "c_" + secrets.token_hex(8)
        self.seq += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[cid] = future
        item = {"id": f"{self.seq}.c", "cmd": cmd, "cid": cid, "args": args}
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
        run = fields.get("run") or new_run_id()
        if run in self.store.records:
            return view(self.store.records[run])
        rec = {
            **{k: fields.get(k) for k in VIEW_FIELDS if k not in ("run", "state", "exit", "cleanup", "rev")},
            "run": run,
            "workspace_id": workspace_id,
            "state": "starting",
            "exit": None,
            "cleanup": None,
            "created_at": _iso(),
            "runner_uid": self.uid,
            "generation": self.generation,
            "dispatched": True,
        }
        await self._save(rec)
        try:
            res = await self.command("spawn", {**payload, "run": run, "workspace_id": workspace_id})
        except HostError as exc:
            if exc.code in ("outcome.unknown", "runner.unavailable"):
                raise HostError(
                    "outcome.unknown", f"the spawn of {run} was sent; the runner did not confirm it"
                ) from None
            await self._save({**self.store.records[run], "state": "exited", "exit": _exit("spawn_failed", exc.code)})
            raise
        cur = self.store.records[run]
        if cur.get("state") == "starting":
            state = res.get("state") if res.get("state") in ("running", "exited") else "running"
            await self._save(
                {**cur, "state": state, "pid": res.get("pid"), "started_at": res.get("started_at") or _iso()}
            )
        return view(self.store.records[run])

    async def stop(self, run: str, grace_ms: int = 10_000) -> dict[str, Any]:
        rec = self.store.records.get(run)
        if rec is None:
            raise HostError("not_found", "no such run")
        if rec.get("state") not in LIVE:
            return {"state": rec.get("state"), "cleanup": rec.get("cleanup") or "unknown"}
        res = await self.command("stop", {"run": run, "grace_ms": grace_ms}, timeout=grace_ms / 1000 + 10)
        cur = self.store.records[run]
        cleanup = res.get("cleanup") if res.get("cleanup") in ("complete", "incomplete", "unknown") else "unknown"
        if cur.get("state") in LIVE or cur.get("cleanup") is None:
            new = {**cur, "cleanup": cleanup}
            if cur.get("state") in LIVE:
                new.update(state="exited", exit=cur.get("exit") or _exit("stopped"))
            await self._save(new)
        return {"state": "exited", "cleanup": cleanup}

    async def attach_allow(self, run: str, person: str, mode: str, *, lease_gen: int = 0) -> dict[str, Any]:
        """Mint a single-use attach ticket (D-P2-25) and register its hash at the runner. Returns ``{att, ticket,
        socket}``; the ticket itself is never stored here."""
        if mode not in ("read", "write"):
            raise HostError("invalid.input", "mode is read or write")
        att = "att_" + secrets.token_hex(8)
        ticket = secrets.token_urlsafe(16)
        expires = int(time.time()) + ATTACH_SECONDS
        await self.command(
            "attach.allow",
            {
                "att": att,
                "ticket_hash": _hash(ticket),
                "run": run,
                "person": person,
                "mode": mode,
                "lease_gen": lease_gen,
                "expires": expires,
            },  # fmt: skip
        )
        self.attachments[att] = {"run": run, "person": person, "mode": mode}
        return {"att": att, "ticket": ticket, "socket": str(self.term_socket)}

    async def attach_end(self, att: str, reason: str) -> None:
        self.attachments.pop(att, None)
        await self.command("attach.end", {"att": att, "reason": reason})

    # -- reads
    def visible(self, rec: dict[str, Any], column: str, person: str) -> bool:
        """P: the caller person's runs; G: that person's agent runs (ticket visibility is checked by the caller)."""
        if rec.get("person") != person:
            return False
        return column == "P" or (column == "G" and rec.get("kind") == "agent")

    def runner_state(self, workspace_id: str) -> dict[str, Any]:
        from orch.runner import enablement

        state = enablement.state_of(self.host.state_dir, workspace_id, self.host.host_uid)
        return {"connected": self.connected, "enabled": state["enabled"], "terminals": state["terminals"]}

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
        for sub in list(self.subs.values()):
            self.end_sub(sub, reason)

    def close_sub(self, session: Session, sid: str) -> bool:
        sub = self.subs.get(sid)
        if sub is None or sub.session is not session:
            return False
        asyncio.get_running_loop().call_soon(self.end_sub, sub, "closed")
        return True

    def open_sub(self, call: Call, seen: set[str]) -> RunSub:
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
        self.subs[sub.id] = sub
        session.subscriptions += 1
        session.on_close.append(lambda: self.end_sub(sub, "closed"))
        sub.task = asyncio.get_running_loop().create_task(self._deliver(sub))
        return sub

    async def _deliver(self, sub: RunSub) -> None:
        """One subscription's items, in order: revalidate the principal, then the run's visibility, then send."""
        try:
            while not sub.ended:
                rec = await sub.queue.get()
                service = self.host.services.get(sub.workspace_id)
                auth = getattr(service, "auth", None)
                if service is None or auth is None:
                    self.end_sub(sub, "reset")
                    return
                reason = self.host.bus.principal_end(sub, auth, service.clock())  # type: ignore[arg-type]
                if reason is not None:
                    self.end_sub(sub, reason)
                    return
                shown = self.visible(rec, sub.column, sub.person)
                if shown and sub.column == "G" and rec.get("ticket"):
                    shown = await asyncio.wrap_future(service.submit(_ticket_visible, service, sub, rec["ticket"]))
                if shown:
                    item = {"id": f"{rec['rev']}.r", "t": "run", "run": view(rec)}
                    sub.seen.add(rec["run"])
                elif rec["run"] in sub.seen:
                    sub.seen.discard(rec["run"])
                    item = {"id": f"{rec['rev']}.r", "t": "gone", "run": rec["run"]}
                else:
                    continue
                if sub.session.closed:
                    self.end_sub(sub, "closed")
                    return
                try:
                    sub.session.send({"t": "evt", "sub": sub.id, "cursor": str(rec["rev"]), "items": [item]})
                except frames.FrameError:
                    self.end_sub(sub, "overflow")
                    return
        except asyncio.CancelledError:
            pass


def _ticket_visible(service: Any, sub: RunSub, key: str) -> bool:
    """On the worker thread: may the grant of ``sub`` see ticket ``key`` now?"""
    from .visibility import Viewer

    try:
        Viewer("G", sub.person).ticket(service.store, key)
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


def new_run_id() -> str:
    """``r_`` + a ULID (48-bit ms time, 80 random bits), Crockford base32."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    out = ""
    for _ in range(26):
        out = _CROCKFORD[n & 31] + out
        n >>= 5
    return "r_" + out


# -- the workspace ops (on the worker thread) ---------------------------------------------------------------------


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
    rev = 0 if hub is None else hub.store.rev
    shown = []
    for rec in records:
        if rec.get("workspace_id") != service.workspace_id or hub is None or not hub.visible(rec, column, person):
            continue
        if rec.get("ticket") and column == "G":
            try:
                viewer.ticket(service.store, rec["ticket"])
            except HostError:
                continue
        shown.append(rec)
    if op == "run.sub":
        return ok_result({"seen": [r["run"] for r in shown], "rev": rev})
    if args.get("state", "live") == "live":
        shown = [r for r in shown if r.get("state") in LIVE]
    if args.get("kind"):
        shown = [r for r in shown if r.get("kind") == args["kind"]]
    if wanted is not None:
        shown = [r for r in shown if r.get("ticket") == wanted]
    shown.sort(key=lambda r: -int(r.get("rev") or 0))
    runner = hub.runner_state(service.workspace_id) if hub is not None else {"connected": False, "enabled": False,
                                                                              "terminals": False}  # fmt: skip
    return ok_result({"runs": [view(r) for r in shown[:256]], "rev": rev, "runner": runner})
