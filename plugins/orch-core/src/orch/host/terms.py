"""The host's terminal ops (H11b, addon #307, caps ``term/1`` and, for ``run.stop``, ``run/1``; §12.3, §12.6).

``term.preview`` and ``term.open`` (a person-run shell: kind ``shell``, ``as_person``, ``ORCH_TICKET`` only, never a
grant or a session), ``term.attach`` and ``attach.cancel``, ``run.stop`` and the ``runner`` part of ``whoami``.
Writes and resizes on a terminal are not host ops: they travel on the runner's term socket under the capability
(:mod:`orch.runner.term`), which re-checks its authorization at every use.

Each op has two halves, like ``run.sub``: :func:`serve` on the workspace worker thread (the column, the store, the
ticket's visibility, the addon, the machine switch: everything that reads the workspace) and :func:`complete` on the
event loop (the runner: spawn, stop, ``attach.allow``). The machine switch is read again in the second half.

**Only the run's own person** may attach, stop or see a run: another person's run is ``not_found`` for P (N5: not
found, also for what the viewer may not see), G and N are refused by the matrix (``op.not_allowed``). The host ends an
attachment by itself when the person leaves, the grant ends, the run's ticket becomes invisible, or a kill switch is
pulled (the addon revoked or disabled, ``orch runner disable``, ``orch runner terminals off``: ``runner.disabled``)
(:meth:`orch.host.runners.RunnerHub._watch`), and on ``attach.cancel``. ``term.attach`` is limited to 30 mints per
minute per person (burst 10; ``rate.limited``). Every retryable refusal (``rate.limited``, ``attach.limit``,
``run.limit``, ``runner.unavailable``) carries ``error.retry_after_ms`` (§0.6); for the rate buckets it is the time
until the person's next token.

**Harnesses** (H11c): ``shell`` (``$SHELL -l``), or ``claude-code``/``codex`` from the closed registry as a
person-run harness (kind ``shell``, ``as_person``, ``ORCH_TICKET`` only: no ``ORCH_GRANT``, no ``ORCH_SESSION``, no
claim precheck, no runner grant; without ``mode`` it starts interactive, ``mode:"context"`` needs a ticket). The plan
is :func:`orch.host.agents.plan` (the same digest rules as ``agent.start``). **Working directory** (H11c): ``cwd``
names the workspace root, a declared repo's working copy or a ticket's registered worktree; without it, with a ticket,
§12.4.4 (never creating a worktree), else the workspace root.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .runners import RunnerHub
    from .workspace import Call, WorkspaceService

__all__ = ["TERM_CAPS", "TERM_OPS", "WRITE_OPS", "addon_granted", "complete", "runner_info", "serve", "stop_op"]

TERM_CAPS = ["term/1"]
TERM_OPS: dict[str, int] = {"term.preview": 1, "term.open": 1, "term.attach": 1, "attach.cancel": 1}
#: the writes (side effects); ``term.preview`` is a read
WRITE_OPS = frozenset({"term.open", "term.attach", "attach.cancel", "run.stop"})
ADDON = "terminals"
#: §0.11: shells per person, runs per runner
SHELLS_PER_PERSON = 16
RUNS_PER_RUNNER = 64

_RUN = {"type": "string", "pattern": r"^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_ATT = {"type": "string", "pattern": r"^at_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_MODEL = {"type": "string", "maxLength": 200}  # checked by the registry: launch.invalid names the field
_KEY = {"type": "string", "minLength": 1, "maxLength": 64}
_CWD = {
    "oneOf": [
        {"type": "object", "properties": {"kind": {"const": "workspace"}}, "required": ["kind"],
         "additionalProperties": False},
        {"type": "object", "properties": {"kind": {"const": "repo"}, "repo": {"type": "string", "maxLength": 64}},
         "required": ["kind", "repo"], "additionalProperties": False},
        {"type": "object", "properties": {"kind": {"const": "worktree"}, "repo": {"type": "string", "maxLength": 64},
                                          "ticket": _KEY}, "required": ["kind", "repo", "ticket"],
         "additionalProperties": False},
        {"type": "object", "properties": {"kind": {"const": "workdir"}, "ticket": _KEY},
         "required": ["kind", "ticket"], "additionalProperties": False},
    ]
}  # fmt: skip


def _obj(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_PREVIEW = {
    "harness": {"enum": ["claude-code", "codex", "shell"]},
    "ticket": _KEY,
    "mode": {"enum": ["context"]},
    "model": _MODEL,
    "cwd": _CWD,
    "label": {"type": "string", "minLength": 1, "maxLength": 40},
}
SCHEMAS: dict[str, dict[str, Any]] = {
    "term.preview": _obj(_PREVIEW),
    "term.open": _obj(
        {**_PREVIEW, "spec": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
         "cols": {"type": "integer", "minimum": 20, "maximum": 500},
         "rows": {"type": "integer", "minimum": 5, "maximum": 200}},
        ("spec",),
    ),
    "term.attach": _obj(
        {"run": _RUN, "mode": {"enum": ["read", "write"]}, "take": {"type": "boolean"},
         "from": {"type": "integer", "minimum": 0}},
        ("run",),
    ),
    "attach.cancel": _obj({"att": _ATT}, ("att",)),
    "run.stop": _obj({"run": _RUN, "grace_ms": {"type": "integer", "minimum": 0, "maximum": 30000}}, ("run",)),
}  # fmt: skip
_VALIDATORS = {op: Draft202012Validator(s) for op, s in SCHEMAS.items()}


def validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def stop_op(op: str) -> bool:
    return op == "run.stop"


# -- the preconditions ---------------------------------------------------------------------------------------------


def addon_granted(service: WorkspaceService) -> bool:
    """The ``terminals`` addon is granted, enabled and not purged in this workspace (on the worker thread)."""
    from . import hubpolicy

    if service.store is None:
        return False
    return hubpolicy.running(service, ADDON)  # granted, on, and not paused by the hub policy


def missing(service: WorkspaceService, hub: RunnerHub, *, addon: bool = True) -> str | None:
    """The first of ``addon``, ``machine``, ``terminals`` that is not in place (§12.3.2), else ``None``."""
    if addon and not addon_granted(service):
        return "addon"
    from orch.runner import enablement

    state = enablement.state_of(hub.host.state_dir, service.workspace_id, hub.host.host_uid)
    if not state["enabled"]:
        return "machine"
    if not state["terminals"]:
        return "terminals"
    return None


def runner_info(service: WorkspaceService, person: str | None = None) -> dict[str, Any]:
    """``whoami.runner`` (``RunnerInfo``): ``grant`` is the caller person's runner grant (H11c), never another's."""
    hub = getattr(service, "runners", None)
    if hub is None:
        return {"connected": False, "enabled": False, "terminals": False, "gen": None, "grant": None}
    info = hub.runner_state(service.workspace_id, person)
    if info["grant"] is not None and person:
        # the host's own launch rule, computed host-side so no client re-derives it (15-minute floor, member scope,
        # revoked grant, role change); `startable_until` is when the floor
        # is reached (clients compare it to their clock)
        from .runnergrants import MIN_LEFT, _epoch, _iso

        cur = hub.grants.current(service.workspace_id, person)
        reason = hub.grants.launch_reason(service, person, cur) if cur else "revoked"
        until = _epoch(info["grant"]["until"])
        info = {
            **info,
            "grant": {
                **info["grant"],
                "startable": reason is None,
                "startable_until": _iso(until - MIN_LEFT) if reason is None and until is not None else None,
            },
        }
    return info


def _require_ready(service: WorkspaceService, hub: RunnerHub | None) -> RunnerHub:
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    gone = missing(service, hub)
    if gone is not None:
        raise HostError(
            "runner.disabled", f"terminals are not available here: {gone} missing", detail={"missing": gone}
        )
    if not hub.connected:
        raise HostError("runner.unavailable", "no runner is connected", retry_after_ms=1000)
    return hub


# -- the spec ------------------------------------------------------------------------------------------------------


def _plan(call: Call, args: dict[str, Any], hub: RunnerHub | None) -> dict[str, Any]:
    """``term.preview``'s answer (and the spec ``term.open`` recomputes): everything the runner will be told."""
    from .agents import plan

    out = plan(call, args, hub, agent=False)
    user = tier_b_user(hub)
    if user is not None:  # spec 13.5: no "shell as me"; the terminal is the agent user's, and the card says so
        out.update(as_person=False, runs_as=user, notice=f"runs as {user}, not as you; it cannot read your keys")
    return out


def tier_b_user(hub: RunnerHub | None) -> str | None:
    """The agent user terminals run as when the host shares its run directory with one (tier B, spec 13.3), else
    ``None`` (tier A: the runner is the person)."""
    share = getattr(getattr(hub, "host", None), "share", None)
    return None if share is None else share.user


def _tier_b_runner_ok(hub: RunnerHub) -> None:
    """In tier B a terminal starts only on a runner that IS the pinned agent user: never a runner with the person's UID
    (it would give a shell as the person), never another UID. Read again at use; fails closed."""
    share = hub.host.share
    if share is None:
        return
    if hub.uid != share.uid or hub.uid == hub.host.host_uid:
        raise HostError("runner.unavailable", "terminals run only on the agent user's runner", retry_after_ms=1000)


# -- the worker half -------------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    """Every op of this module on the workspace worker thread; the column was checked by the gate."""
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    validate(op, args)
    opts = call.frame.get("opts") or {}
    if opts.get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    hub: RunnerHub | None = getattr(service, "runners", None)
    person = str(call.viewer)
    if op == "term.preview":
        plan = _plan(call, args, hub)
        plan.pop("_launch")
        return ok_result(plan)
    if op == "term.open":
        if not opts.get("idem"):
            raise HostError("idem.required", "term.open needs opts.idem")
        plan = _plan(call, args, hub)
        if plan["blocked"] is not None:
            raise HostError(plan["blocked"]["code"], plan["blocked"]["message"], detail=plan["blocked"].get("detail"))
        if plan["spec"] != args["spec"]:
            raise HostError("launch.changed", "what term.open would start differs from the preview: preview again")
        return ok_result(
            {"launch": plan["_launch"], "idem": str(opts["idem"]), "person": person, "label": args.get("label"),
             "cols": args.get("cols", 120), "rows": args.get("rows", 40), "harness": args.get("harness", "shell"),
             "mode": args.get("mode"), "model": args.get("model"), "cwd": plan["cwd"]}
        )  # fmt: skip
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    if op == "attach.cancel":
        info = hub.attachments.get(args["att"])
        mine = info is not None and info.get("person") == person and info.get("workspace_id") == service.workspace_id
        return ok_result({"att": args["att"] if mine else None, "person": person})
    rec = hub.store.records.get(args["run"])
    if rec is None or rec.get("workspace_id") != service.workspace_id or rec.get("person") != person:
        raise HostError("not_found", "no such run")  # N5: also another person's run
    if rec.get("ticket"):
        Viewer.of(call).ticket(service.store, rec["ticket"])  # a ticket the person cannot see: not_found
    if op == "run.stop":
        return ok_result({"run": args["run"], "grace_ms": args.get("grace_ms", 10_000), "person": person})
    _require_ready(service, hub)
    who = {"column": call.column.name, "uid": call.peer.uid, "grant": None, "grant_id": None}
    return ok_result({"run": args["run"], "mode": args.get("mode", "read"), "take": bool(args.get("take")),
                      "from": args.get("from"), "person": person, "who": who})  # fmt: skip


# -- the event-loop half ---------------------------------------------------------------------------------------------


async def complete(call: Call, checked: dict[str, Any]) -> dict[str, Any]:
    from .workspace import ok_result

    op = call.frame["op"]
    data = checked["data"]
    hub: RunnerHub | None = getattr(call.service, "runners", None)
    if op == "term.preview":
        return checked
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    if op == "term.open":
        return ok_result({"run": await _open(call, hub, data)})
    if op == "attach.cancel":
        if data["att"] is not None:
            try:
                await hub.attach_end(data["att"], "cancelled")
            except HostError:
                pass  # no runner: nothing is attached there any more
        return ok_result({})
    if op == "run.stop":
        return ok_result(await _stop(hub, data))
    return ok_result(await _attach(call, hub, data))


async def _open(call: Call, hub: RunnerHub, data: dict[str, Any]) -> dict[str, Any]:
    from . import runners

    wid = call.service.workspace_id
    person = data["person"]
    run = "r_" + data["idem"][2:]  # the idem names the run: a retry finds the record instead of a second process
    _tier_b_runner_ok(hub)
    existing = hub.store.records.get(run)
    if existing is not None:
        if existing.get("workspace_id") != wid or existing.get("person") != person:
            raise HostError("idem.conflict", "this idem names another request")
        return runners.view(existing)
    from orch.runner import enablement

    state = enablement.state_of(hub.host.state_dir, wid, hub.host.host_uid)  # read again at use
    if not state["enabled"] or not state["terminals"]:
        raise HostError(
            "runner.disabled", "terminals are not available here: machine missing", detail={"missing": "machine"}
        )
    live = [r for r in hub.store.records.values() if r.get("state") in runners.LIVE]
    if sum(1 for r in live if r.get("person") == person and r.get("kind") == "shell") >= SHELLS_PER_PERSON:
        raise HostError(
            "run.limit", "at most 16 shells per person", retry_after_ms=5000, detail={"which": "shell_person"}
        )
    if sum(1 for r in live if r.get("runner_uid") == hub.uid) >= RUNS_PER_RUNNER:
        raise HostError("run.limit", "at most 64 runs per runner", retry_after_ms=5000, detail={"which": "runner"})
    if not hub.take_start(person):
        raise HostError("rate.limited", "at most 10 starts per minute", retry_after_ms=hub.start_retry_ms(person))
    launch = data["launch"]
    fields = {"run": run, "kind": "shell", "person": person, "ticket": launch["ticket"],
              "harness": data.get("harness", "shell"), "mode": data.get("mode"), "model": data.get("model"),
              "label": data.get("label"), "as_person": tier_b_user(hub) is None,
              "cwd": data.get("cwd") or {"kind": "workspace"}}  # fmt: skip
    if launch.get("mkdir"):
        from .agents import make_workdir

        where = data.get("cwd") or {}
        launch["cwd"] = make_workdir(call, where.get("ticket") or launch["ticket"] or "", None)
    payload = {"kind": "shell", "argv": launch["argv"], "env_names": launch["env_names"],
               "env_fixed": launch["env_fixed"], "cwd": launch["cwd"], "cols": data["cols"], "rows": data["rows"],
               "login_shell": launch["login_shell"], "grant": None, "session": None}  # fmt: skip
    return await hub.spawn(wid, fields, payload)


async def _stop(hub: RunnerHub, data: dict[str, Any]) -> dict[str, Any]:
    run = data["run"]
    res = await hub.stop(run, data["grace_ms"])
    hub._audit("run.stop", run=run, person=data["person"], state=res.get("state"), cleanup=res.get("cleanup"))
    state = "exited" if res.get("state") in ("exited", "lost") else "stopping"
    return {"state": state, "cleanup": res.get("cleanup") if state == "exited" else None}


async def _attach(call: Call, hub: RunnerHub, data: dict[str, Any]) -> dict[str, Any]:
    run, person, mode = data["run"], data["person"], data["mode"]
    from orch.runner import enablement

    state_now = enablement.state_of(hub.host.state_dir, call.service.workspace_id, hub.host.host_uid)
    if not state_now["enabled"] or not state_now["terminals"]:  # the switch is read again at use
        raise HostError(
            "runner.disabled", "terminals are not available here: machine missing", detail={"missing": "machine"}
        )
    rec = hub.store.records.get(run)
    if rec is None or rec.get("person") != person:
        raise HostError("not_found", "no such run")
    state = rec.get("state")
    if state == "running":
        pass
    elif state == "exited" and mode == "read" and _replayable(rec):
        pass
    else:
        raise HostError(
            "term.not_attachable", f"the run is {state}: nothing can be attached to it", detail={"state": state}
        )
    if rec.get("workspace_id") != call.service.workspace_id:
        raise HostError("not_found", "no such run")
    if not hub.connected or rec.get("runner_uid") != hub.uid or rec.get("gen") != hub.generation:
        raise HostError("runner.unavailable", "the run's runner is not connected", retry_after_ms=1000)
    per_run, per_person, total = hub.counts(run, person)
    limit_run, limit_person, limit_runner = runners_limits()
    if per_run >= limit_run or per_person >= limit_person or total >= limit_runner:
        raise HostError("attach.limit", "attachment limit reached (4 per run, 16 per person, 64 per runner)",
                        retry_after_ms=2000)  # fmt: skip
    held = hub.term_state.get(run) or {"lease_gen": 0, "atts": []}
    if mode == "write" and not data["take"] and any(a["writer"] for a in held["atts"]):
        raise HostError("term.writer_busy", "another attachment holds the write lease: take it or attach read-only")
    if not hub.take_mint(person):
        raise HostError(
            "rate.limited", "at most 30 terminal attaches per minute", retry_after_ms=hub.mint_retry_ms(person)
        )
    got = await hub.attach_allow(run, person, mode, take=data["take"], lease_gen=held["lease_gen"],
                                 start_from=data["from"], who=data["who"])  # fmt: skip
    return got


def _replayable(rec: dict[str, Any]) -> bool:
    """An exited run's ring is kept by the runner for an hour (``Run.replay_until``)."""
    from .runners import REPLAY_SECONDS

    ended = float(rec.get("ended_ts") or 0)
    return (
        (rec.get("exit") or {}).get("reason") in ("exited", "stopped")
        and 0 < ended
        and (time.time() - ended < REPLAY_SECONDS)
    )


def runners_limits() -> tuple[int, int, int]:
    from orch.runner.term import PER_PERSON, PER_RUN, PER_RUNNER

    return PER_RUN, PER_PERSON, PER_RUNNER
