"""Start an agent (H11c, #307, D-P2-27, D-P2-28, D-P2-31, D-P2-37; §12.4): ``agent.preview``, ``agent.start``, and the
launch plan ``term.preview``/``term.open`` share (:func:`plan`).

**The plan** is everything the runner will be told, computed by core from validated values only (D-P2-28): the
harness of the closed registry (:mod:`orch.host.launch`), the mode, model and subagent model, the ticket key the host
looked up, the working directory (§12.4.4), the environment names and host-chosen values (§12.4.3) and, for an agent,
the person's runner grant. Its ``spec`` digest covers harness, mode, model, subagent model, argv, the cwd pin (or the
worktree to be created, with its registration generation), the environment names and fixed values and the grant id.
``agent.preview`` has no side effects. ``agent.start {…, spec}`` recomputes the plan and refuses ``launch.changed``
on any difference, then (on the event loop) persists the launch intent, creates the ticket's worktree when the plan
says so, and sends ``spawn``. The run id is derived from ``opts.idem``, so a retry finds its record and never starts a
second process; the runner dedupes ``spawn`` by run id as well.

**Order of ``blocked``** (agent): the ticket's visibility (``not_found``), the addon ``start-agent`` and the machine
switch (``runner.disabled``, the first missing piece named), the runner connection (``runner.unavailable``), a live
claim on the ticket (``launch.claimed``), harness, mode, model and shell (``launch.invalid``, ``harness.*``), the
working directory (``worktree.*``, ``launch.cwd_changed``, ``observe.unavailable``), the runner grant
(``launch.no_grant`` with ``reason`` ``none``, ``expiring``, ``scope`` or ``revoked``). The terminal switch is not
asked: ``--no-terminals`` is a preference, it does not make agent execution safe (D-P2-31).

**Columns**: ``agent.preview`` and ``agent.start`` are P only (G and N: ``op.not_allowed`` at the gate, a request with
``auth.grant`` included). The agent claims the ticket itself.

**Working directory** (§12.4.4), in order: the ticket's registered worktree for its primary repo (the first of
``links.repos``; created by ``agent.start`` when ``worktree:true``, the ticket links a branch for it and the
``worktrees`` addon is granted), the declared working copy (a warning says "shared working copy"). A ticket with no
codebase (it links no repo, or none that is declared) gets its own **work folder** ``<project>/.orch-work/<KEY>/``
(kind ``workdir``; this module is the one place that decides codebase or not): made 0700 by ``agent.start`` below the
project fd without following a link, pinned, ``ORCH_WORKDIR`` names it, and it is in the spec digest. The workspace
root remains for a ticketless shell.
It is sent as a pin ``{path, dev, ino}``; the trampoline compares it before ``fchdir`` (``launch.cwd_changed``).
``ORCH_WORKTREE`` names the worktree cwd; ``ORCH_WORKTREES`` lists the ticket's other registered worktrees as
``repo=path`` pairs separated by ``:`` (a path containing ``:`` is left out and named in ``warnings``).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError
from .launch import HARNESSES, LOGIN_NOTICE, check_model, display, launch_argv
from .visibility import Viewer

if TYPE_CHECKING:
    from .runners import RunnerHub
    from .workspace import Call, WorkspaceService

__all__ = ["AGENT_OPS", "ADDON", "complete", "plan", "serve"]

AGENT_OPS: dict[str, int] = {"agent.preview": 1, "agent.start": 1}
ADDON = "start-agent"
#: §0.11 (v1.6): 8 agent runs per person per workspace, 16 per person, 64 per runner
AGENTS_PER_WORKSPACE = 8
AGENTS_PER_PERSON = 16
RUNS_PER_RUNNER = 64
SHARED_COPY = "shared working copy: other runs and you work in the same directory"
_KEY = {"type": "string", "minLength": 1, "maxLength": 64}
_FREE = {"type": "string", "maxLength": 200}  # checked by the registry (launch.invalid names the field)
_PREVIEW = {
    "ticket": _KEY,
    "harness": {"enum": ["claude-code", "codex"]},
    "mode": {"enum": ["refine", "work", "fix", "continue", "context"]},
    "model": _FREE,
    "subagent_model": _FREE,
    "label": {"type": "string", "minLength": 1, "maxLength": 40},
    "worktree": {"type": "boolean"},
}
_SCHEMAS = {
    "agent.preview": {"type": "object", "properties": _PREVIEW, "required": ["ticket", "harness", "mode"],
                      "additionalProperties": False},
    "agent.start": {"type": "object",
                    "properties": {**_PREVIEW, "spec": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                   "cols": {"type": "integer", "minimum": 20, "maximum": 500},
                                   "rows": {"type": "integer", "minimum": 5, "maximum": 200}},
                    "required": ["ticket", "harness", "mode", "spec"], "additionalProperties": False},
}  # fmt: skip
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def _addon(service: WorkspaceService, name: str) -> bool:
    from . import hubpolicy

    return hubpolicy.running(service, name)  # granted, on, and not paused by the hub policy


def agent_missing(service: WorkspaceService, hub: RunnerHub) -> str | None:
    """The first of ``addon`` (``start-agent``), ``machine`` that is not in place, else ``None``."""
    from orch.runner import enablement

    if not _addon(service, ADDON):
        return "addon"
    if not enablement.state_of(hub.host.state_dir, service.workspace_id, hub.host.host_uid)["enabled"]:
        return "machine"
    return None


def _shell_ok() -> bool:
    """A hint for the preview only: the host's own ``SHELL`` when it is set (tier A: the person's account); the runner
    checks its own ``SHELL`` at every spawn and is authoritative (``harness.shell_unsupported``)."""
    from orch.runner.link import POSIX_SHELLS

    shell = os.environ.get("SHELL")
    return shell is None or os.path.basename(shell) in POSIX_SHELLS


def _pin(path: str) -> dict[str, Any]:
    st = os.stat(path)
    return {"path": path, "dev": st.st_dev, "ino": st.st_ino}


class _Plan:
    """Collects the first ``blocked`` and the warnings while a plan is computed."""

    def __init__(self) -> None:
        self.blocked: dict[str, Any] | None = None
        self.warnings: list[str] = []

    def block(self, code: str, message: str, **detail: Any) -> None:
        if self.blocked is None:
            self.blocked = {"code": code, "message": message[:400], **({"detail": detail} if detail else {})}


def _git(call: Call) -> Any:
    git = call.service.caps_for(call.principal).git
    return git if git.enabled and git.reader is not None else None


def _cwd(call: Call, view: Any, cwd_arg: dict[str, Any] | None, *, agent: bool, want_worktree: bool,
         p: _Plan) -> dict[str, Any]:  # fmt: skip
    """§12.4.4: ``{cwd, path, pin, create}``; ``create`` is the worktree plan when ``agent.start`` creates it."""
    service = call.service
    store = service.store
    root = str(service.entry.root)
    out: dict[str, Any] = {"cwd": {"kind": "workspace"}, "path": root, "pin": None, "create": None, "other": {},
                           "mkdir": False}  # fmt: skip

    def at_root() -> dict[str, Any]:
        out.update(cwd={"kind": "workspace"}, path=root, create=None, mkdir=False)
        try:
            out["pin"] = _pin(root)
        except OSError:
            out["pin"] = {"path": root, "dev": 0, "ino": 0}
            p.block("launch.cwd_changed", "the workspace root cannot be read")
        return out

    def declared(repo: str) -> dict[str, Any] | None:
        git = _git(call)
        path = git.repo_path(store.layout, store.state.workspace.repos, repo) if git is not None else None
        if path is None:
            return None
        out.update(cwd={"kind": "repo", "repo": repo}, path=os.fspath(path), create=None)
        try:
            out["pin"] = _pin(os.fspath(path))
        except OSError:
            out["pin"] = {"path": os.fspath(path), "dev": 0, "ino": 0}
            p.block("launch.cwd_changed", f"{repo}: the declared working copy cannot be read")
        return out

    def workdir(key: str) -> dict[str, Any]:
        """No codebase: the ticket's work folder (absent: ``mkdir``, made by the start; present: pinned)."""
        from orch.layout import LayoutError, workdir_stat, workdir_state

        lay = store.layout
        try:
            st = workdir_stat(lay, key)
            out["workdir"] = workdir_state(lay, key)
            path = os.fspath(lay.workdir(key))
        except LayoutError as exc:
            p.block("launch.cwd_changed", f"{key}: the work folder cannot be used: {exc}"[:300])
            return at_root()
        out.update(cwd={"kind": "workdir", "ticket": key}, path=path, create=None, mkdir=st is None,
                   pin=None if st is None else {"path": path, "dev": st.st_dev, "ino": st.st_ino})  # fmt: skip
        return out

    def registered(git: Any, uid: str, key: str, repo: str) -> bool:
        slot = git.reader.pins.worktree(uid, repo)
        reg = slot["reg"]
        if reg is None:
            return False
        if reg["pins"] is None:
            p.block("worktree.unpinned", f"{key}: the worktree for {repo} is not pinned; remove it and create it again")
        pin = (reg["pins"] or {}).get("worktree") or {"path": reg["path"], "dev": 0, "ino": 0}
        pinned = {"path": reg["path"], "dev": pin.get("dev", 0), "ino": pin.get("ino", 0)}
        out.update(cwd={"kind": "worktree", "repo": repo, "ticket": key}, path=reg["path"], create=None, pin=pinned,
                   gen=slot["gen"])  # fmt: skip
        return True

    if cwd_arg is not None and cwd_arg["kind"] == "workspace":
        return at_root()
    if cwd_arg is not None and cwd_arg["kind"] == "repo":
        if cwd_arg["repo"] not in store.state.workspace.repos or declared(cwd_arg["repo"]) is None:
            p.block("not_found", f"no declared repo {cwd_arg['repo'][:64]}")
            return at_root()
        return out
    if cwd_arg is not None and cwd_arg["kind"] == "workdir":  # "Open terminal here" of a no-codebase ticket
        try:
            wview = Viewer.of(call).ticket(store, cwd_arg["ticket"])
        except HostError as exc:
            p.block(exc.code, exc.message)
            return at_root()
        return workdir(wview.key)
    if cwd_arg is not None:  # a named worktree (term.preview "Open terminal here")
        git = _git(call)
        try:
            wview = Viewer.of(call).ticket(store, cwd_arg["ticket"])
        except HostError as exc:
            p.block(exc.code, exc.message)
            return at_root()
        if git is None:
            p.block("observe.unavailable", "git cannot be read in this workspace now")
            return at_root()
        if not registered(git, wview.uid, wview.key, cwd_arg["repo"]):
            p.block("worktree.none", f"{wview.key} has no worktree for {cwd_arg['repo'][:64]}")
            return at_root()
        return out
    if view is None:
        return at_root()
    from orch.ops.workdir import has_codebase

    repos = list((view.fields.get("links") or {}).get("repos") or [])
    if not has_codebase(store, view):  # the one rule, shared with the ops: no declared primary repo, no codebase
        return workdir(view.key)
    git = _git(call)
    if git is None:
        p.block("observe.unavailable", "the ticket links a repo, and git cannot be read in this workspace now")
        return at_root()
    primary = repos[0]
    for repo in repos[1:]:  # ORCH_WORKTREES: the ticket's other registered worktrees
        reg = git.reader.pins.worktree(view.uid, repo)["reg"]
        if reg is not None and reg.get("pins") is not None:
            out["other"][repo] = reg["path"]
    if want_worktree and registered(git, view.uid, view.key, primary):
        return out
    branch = ((view.fields.get("links") or {}).get("branches") or {}).get(primary)
    if want_worktree and agent and branch:
        from . import worktrees

        try:
            wplan = worktrees._plan(call, "worktree.create", {"ticket": view.key, "repo": primary}, None)
        except HostError as exc:
            if exc.code == "runner.disabled":  # the worktrees addon is not granted: no worktree is created
                p.warnings.append("the worktrees addon is not granted: the agent works in the shared working copy")
            else:
                p.block(exc.code, exc.message)
                return at_root()
        else:
            out.update(cwd={"kind": "worktree", "repo": primary, "ticket": view.key}, path=wplan["path"], pin=None,
                       create=wplan, gen=wplan["gen"])  # fmt: skip
            return out
    if declared(primary) is None:  # declared but not readable now: refuse, never a silent work folder
        p.block("launch.cwd_changed", f"{primary}: the declared working copy cannot be read")
        return at_root()
    p.warnings.append(SHARED_COPY)
    return out


def plan(call: Call, args: dict[str, Any], hub: RunnerHub | None, *, agent: bool) -> dict[str, Any]:
    """The preview's answer and, under ``_launch``, what start or open sends. ``agent``: ``agent.preview``;
    otherwise ``term.preview`` (a person-run shell or harness: kind ``shell``, ``ORCH_TICKET`` only, no grant)."""
    from orch.runner.config import ConfigError, RunnerConfig, read_config
    from orch.runner.link import ENV_FROM_RUNNER

    from .terms import missing

    service = call.service
    p = _Plan()
    harness = args.get("harness", "shell")
    mode, model, sub = args.get("mode"), args.get("model"), args.get("subagent_model")
    person = str(call.viewer)
    view = None
    if args.get("ticket"):
        try:
            view = Viewer.of(call).ticket(service.store, args["ticket"])
        except HostError as exc:
            p.block(exc.code, exc.message)
    key = view.key if view is not None else None
    if hub is None:
        p.block("runner.unavailable", "this host serves no runner")
    else:
        gone = agent_missing(service, hub) if agent else missing(service, hub)
        if gone is not None:
            p.block("runner.disabled", f"dashboard execution is not available here: {gone} missing", missing=gone)
        elif not hub.connected:
            p.block("runner.unavailable", "no runner is connected")
        else:
            try:
                hub.require_current()
            except HostError as exc:
                p.block(exc.code, exc.message)
    if agent and view is not None and view.claim is not None and view.claim.live:
        p.block("launch.claimed", f"{view.key} is claimed by a live session; the agent could not claim it")
    try:
        config = read_config(hub.host.state_dir if hub is not None else service.state_dir, service.host_uid)
    except ConfigError as exc:
        config = RunnerConfig()
        p.block("launch.invalid", str(exc))
    source, routed_by = ("chosen" if model is not None else "default"), None
    if agent and model is None and mode is not None and harness in HARNESSES:
        # an add-on that holds choose_model proposes; core checks the answer against its closed list, and the person's
        # review binds the result (it is in the spec digest). An explicit model never asks the add-on.
        from . import routing

        routed = routing.choose(service, harness, mode)
        if routed.problem is not None:
            p.block("launch.invalid", routed.problem)
        elif routed.model is not None:
            model, source, routed_by = routed.model, "addon", routed.by
    for name, value, of in (("model", model, harness), ("subagent_model", sub, "claude-code")):
        bad = check_model(name, value, of)
        if bad:
            p.block("launch.invalid", bad)
    if sub is not None and harness != "claude-code":
        p.block("launch.invalid", "subagent_model is for claude-code only")
    if mode is not None and key is None and p.blocked is None:
        p.block("launch.invalid", f"mode {mode} needs a ticket")
    argv_model = model if not check_model("model", model, harness) else None  # never an unchecked name in an argv
    argv, login, program = ["$SHELL", "-l"], False, None
    try:
        argv, login, program = launch_argv(harness, mode, argv_model, key, config)
    except HostError as exc:
        p.block(exc.code, exc.message)
    if login and not _shell_ok():
        p.block("harness.shell_unsupported", "your $SHELL is not a POSIX shell (sh, bash, zsh, dash, ksh)")
    if program is not None and not os.access(program, os.X_OK):
        p.block("harness.not_found", f"{program} is not an executable program")
    if login:
        p.warnings.append(LOGIN_NOTICE)
    want_worktree = bool(args.get("worktree", True)) if agent else True
    where = _cwd(call, view, None if agent else args.get("cwd"), agent=agent, want_worktree=want_worktree, p=p)
    env_names = sorted(ENV_FROM_RUNNER | set(config.pass_env))
    env_fixed: dict[str, str] = {}
    if key is not None:
        env_fixed["ORCH_TICKET"] = key
    if agent:
        env_fixed["ORCH_HARNESS"] = harness
        # The agent's cwd may be a worktree or a linked repo, outside the project: discovery cannot walk up to the
        # workspace from there. The host names it (its own registered root, never a client value); the grant is
        # bound to this workspace, so an agent that points ORCH_WORKSPACE elsewhere is refused by that other host.
        env_fixed["ORCH_WORKSPACE"] = str(service.entry.root)
        if where["cwd"]["kind"] == "worktree":
            env_fixed["ORCH_WORKTREE"] = where["path"]
        elif where["cwd"]["kind"] == "workdir":
            env_fixed["ORCH_WORKDIR"] = where["path"]
        pairs = []
        for repo, path in sorted(where["other"].items()):
            if ":" in path:
                p.warnings.append(f"ORCH_WORKTREES leaves out {repo}: its path contains ':'")
                continue
            pairs.append(f"{repo}={path}")
        if pairs:
            env_fixed["ORCH_WORKTREES"] = ":".join(pairs)
        if sub is not None and harness == "claude-code" and not check_model("subagent_model", sub, "claude-code"):
            env_fixed["CLAUDE_CODE_SUBAGENT_MODEL"] = sub
    if agent and mode is not None and where["cwd"]["kind"] != "workspace":
        # AGENTS.orch.md lives in the project's store, not in the worktree: the prompt names it by absolute path
        try:
            from orch.instructions.harness import agents_rel

            lay = service.store.layout
            at = os.path.join(str(service.entry.root), agents_rel(lay.project, lay.store))
            argv, login, program = launch_argv(
                harness, mode, argv_model, key, config,
                instructions=at if at.isascii() and at.isprintable() else None,
                workdir=where["cwd"]["kind"] == "workdir",
            )  # fmt: skip
        except HostError:
            pass  # the first launch_argv already blocked with the same reason
    grant = None
    if agent:
        cur = hub.grants.current(service.workspace_id, person) if hub is not None else None
        if cur is None:
            p.block("launch.no_grant", "no runner grant for you on this machine: issue one", reason="none")
        else:
            reason = hub.grants.launch_reason(service, person, cur)  # type: ignore[union-attr]
            if reason is not None:
                p.block("launch.no_grant", f"your runner grant cannot start agents now ({reason})", reason=reason)
            else:
                grant = {"id": cur["grant"], "until": cur["until"], "scope": cur["scope"]}
    if where.get("mkdir"):  # the work folder is made by the start: the digest binds where, and that it is new
        pin_view = {"create_workdir": True, "path": where["path"]}
    elif where["create"] is None:
        pin_view = where["pin"]
    else:
        pin_view = {
            "create": True, "path": where["path"], "gen": where["create"]["gen"],
            "branch": where["create"]["branch"], "base": where["create"].get("base_sha"),
        }  # fmt: skip
    digest = hashlib.sha256(
        json.dumps(
            {"v": 2, "kind": "agent" if agent else "shell", "harness": harness, "mode": mode, "model": model,
             "model_source": source, "routed_by": routed_by, "subagent_model": sub, "argv": argv, "login_shell": login,
             "cwd": where["cwd"], "pin": pin_view, "gen": where.get("gen"), "env_names": env_names,
             "env_fixed": env_fixed,
             "grant": grant["id"] if grant else None, "as_person": not agent},
            sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()  # fmt: skip
    shown_env = sorted(
        {*env_names, *env_fixed, "TERM", "COLORTERM", *(("ORCH_GRANT", "ORCH_SESSION") if agent else ())}
    )
    out: dict[str, Any] = {
        "spec": digest, "model": model, "model_source": source, "model_by": routed_by, "argv": argv,
        "display": display(argv), "login_shell": login, "program": program, "cwd": where["cwd"],
        "cwd_path": where["path"],
        "env": shown_env, "blocked": p.blocked, "warnings": p.warnings[:8],
        "_launch": {"argv": argv, "env_names": env_names, "env_fixed": env_fixed, "cwd": where["pin"],
                    "create": where["create"], "mkdir": where["mkdir"], "ticket": key, "login_shell": login,
                    "grant": grant["id"] if grant else None},
    }  # fmt: skip
    if agent:
        out.update(ticket=key, will_create_worktree=where["create"] is not None, grant=grant)
        if where.get("workdir") is not None:  # a new ticket's folder must be new or empty for an unattended start
            out["workdir"] = where["workdir"]
    else:
        out.update(as_person=True, notice="acts as you, not as an agent")
    return out


# -- the worker half ---------------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    opts = call.frame.get("opts") or {}
    if opts.get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op == "agent.start" and service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    service.store.refresh()
    hub: RunnerHub | None = getattr(service, "runners", None)
    if op == "agent.preview":
        out = plan(call, args, hub, agent=True)
        out.pop("_launch")
        return ok_result(out)
    idem = opts.get("idem")
    if not idem:
        raise HostError("idem.required", "agent.start needs opts.idem")
    run = "r_" + str(idem)[2:]
    person = str(call.viewer)
    if hub is not None and run in hub.store.records:  # a retry: the recorded run, nothing recomputed or started
        _same(hub.store.records[run], service.workspace_id, person, args["spec"])
        return ok_result({"replay": run})
    found = plan(call, args, hub, agent=True)
    if found["blocked"] is not None:
        raise HostError(found["blocked"]["code"], found["blocked"]["message"], detail=found["blocked"].get("detail"))
    if found["spec"] != args["spec"]:
        raise HostError("launch.changed", "what agent.start would start differs from the preview: preview again")
    return ok_result({
        "run": run, "idem": str(idem), "spec": found["spec"], "launch": found["_launch"], "person": person,
        "harness": args["harness"], "mode": args["mode"], "model": found["model"],
        "subagent_model": args.get("subagent_model"), "label": args.get("label"), "cwd": found["cwd"],
        "cols": args.get("cols", 120), "rows": args.get("rows", 40),
    })  # fmt: skip


# -- the event-loop half ----------------------------------------------------------------------------------------------


async def complete(call: Call, checked: dict[str, Any]) -> dict[str, Any]:
    from . import runners
    from .workspace import ok_result

    op = call.frame["op"]
    if op == "agent.preview":
        return checked
    data = checked["data"]
    hub: RunnerHub | None = getattr(call.service, "runners", None)
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    if "replay" in data:
        return ok_result({"run": runners.view(hub.store.records[data["replay"]])})
    return ok_result({"run": await _start(call, hub, data)})


def _same(rec: dict[str, Any], wid: str, person: str, spec: str) -> None:
    """An idem names exactly one agent start: the same workspace, person, kind and spec (Opus L3)."""
    if (
        rec.get("workspace_id") != wid
        or rec.get("person") != person
        or rec.get("kind") != "agent"
        or rec.get("spec") != spec
    ):
        raise HostError("idem.conflict", "this idem names another request")


async def _start(call: Call, hub: RunnerHub, data: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    from . import runners

    service = call.service
    wid, person, run = service.workspace_id, data["person"], data["run"]
    gone = await asyncio.wrap_future(service.submit(agent_missing, service, hub))  # again, at use
    if gone is not None:
        raise HostError("runner.disabled", "dashboard execution is not available here", detail={"missing": gone})
    # from here to the reservation there is no await: the identity check, the limits, the rate charge and the
    # reservation are one step, so two requests with one idem never both start (Codex P2 #1, Opus L2)
    existing = hub.store.records.get(run)
    if existing is not None:
        _same(existing, wid, person, data["spec"])
        return runners.view(existing)
    starting = hub.starting.get(run)
    if starting is not None:  # the same idem is being started right now: its answer is this one's too
        spec, future = starting
        if spec != data["spec"]:
            raise HostError("idem.conflict", "this idem names another request")
        await asyncio.shield(future)
        rec = hub.store.records.get(run)
        if rec is None:  # the first start failed before its record existed (Opus info): its outcome is not ours
            raise HostError("outcome.unknown", "the same request was being started and did not record a run; retry")
        _same(rec, wid, person, data["spec"])
        return runners.view(rec)
    if not hub.connected:
        raise HostError("runner.unavailable", "no runner is connected", retry_after_ms=1000)
    hub.require_current()
    launch = data["launch"]
    cur = hub.grants.current(wid, person)
    if cur is None or cur["grant"] != launch["grant"]:
        raise HostError("launch.changed", "the runner grant changed since the preview: preview again")
    live = [r for r in hub.store.records.values() if r.get("state") in runners.LIVE]
    mine = [r for r in live if r.get("person") == person and r.get("kind") == "agent"]
    if sum(1 for r in mine if r.get("workspace_id") == wid) >= AGENTS_PER_WORKSPACE or len(mine) >= AGENTS_PER_PERSON:
        which = (
            "agent_ws"
            if sum(1 for r in mine if r.get("workspace_id") == wid) >= AGENTS_PER_WORKSPACE
            else "agent_person"
        )
        raise HostError(
            "run.limit", "at most 8 agent runs per person in a workspace, 16 in all", retry_after_ms=5000,
            detail={"which": which},
        )  # fmt: skip
    if sum(1 for r in live if r.get("runner_uid") == hub.uid) >= RUNS_PER_RUNNER:
        raise HostError("run.limit", "at most 64 runs per runner", retry_after_ms=5000, detail={"which": "runner"})
    if not hub.take_start(person):
        raise HostError("rate.limited", "at most 10 starts per minute", retry_after_ms=6000)
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    hub.starting[run] = (data["spec"], future)
    try:
        return await _launch(call, hub, data)
    finally:
        hub.starting.pop(run, None)
        future.set_result(None)


async def _launch(call: Call, hub: RunnerHub, data: dict[str, Any]) -> dict[str, Any]:
    from . import runners

    wid, person, run = call.service.workspace_id, data["person"], data["run"]
    launch = data["launch"]
    session = "s_" + runners.new_ulid()
    fields = {"run": run, "kind": "agent", "person": person, "ticket": launch["ticket"], "harness": data["harness"],
              "mode": data["mode"], "model": data["model"], "subagent_model": data["subagent_model"],
              "label": data["label"], "session": session, "grant": launch["grant"], "grant_state": "ok",
              "as_person": False, "cwd": data["cwd"]}  # fmt: skip
    payload = {"kind": "agent", "argv": launch["argv"], "env_names": launch["env_names"],
               "env_fixed": launch["env_fixed"], "cwd": launch["cwd"], "cols": data["cols"], "rows": data["rows"],
               "login_shell": launch["login_shell"], "grant": launch["grant"], "session": session,
               "person": person}  # fmt: skip
    await hub.intent(wid, fields, payload, idem=data["idem"], spec=data["spec"])
    hub.grants.watch()
    if launch.get("mkdir"):
        try:
            payload["cwd"] = make_workdir(call, launch["ticket"], launch["env_fixed"].get("ORCH_WORKDIR"))
        except HostError as exc:
            await hub.fail(run, exc.code)
            hub._audit("run.outcome", run=run, outcome="spawn_failed", code=exc.code)
            raise
    if launch["create"] is not None:
        try:
            payload["cwd"] = await _create_worktree(call, launch["create"])
        except HostError as exc:
            await hub.fail(run, exc.code)
            hub._audit("run.outcome", run=run, outcome="spawn_failed", code=exc.code)
            raise
        if payload["env_fixed"].get("ORCH_WORKTREE") != payload["cwd"]["path"]:
            await hub.fail(run, "launch.cwd_changed")
            raise HostError("launch.cwd_changed", "the created worktree is not where the preview said")
    return await hub.dispatch(run, payload, audit=True)


def make_workdir(call: Call, key: str, expect: str | None) -> dict[str, Any]:
    """Make the ticket's work folder (0700, no link followed) and return the cwd pin. ``expect`` is the path the
    preview bound (``ORCH_WORKDIR`` when set): a different path is ``launch.cwd_changed``."""
    from orch.layout import LayoutError, ensure_workdir

    lay = call.service.store.layout
    try:
        path = os.fspath(lay.workdir(key))
        st = ensure_workdir(lay, key)
    except LayoutError as exc:
        raise HostError("launch.cwd_changed", f"{key}: the work folder cannot be used: {exc}"[:300]) from None
    if expect is not None and expect != path:
        raise HostError("launch.cwd_changed", "the work folder is not where the preview said")
    return {"path": path, "dev": st.st_dev, "ino": st.st_ino}


async def _create_worktree(call: Call, wplan: dict[str, Any]) -> dict[str, Any]:
    """H11d's create under the per-repo lock (recheck, runner git, registration), then the new registration's pin."""
    import asyncio

    from . import worktrees

    await worktrees.execute(call, {"plan": wplan})

    def pinned() -> dict[str, Any]:
        git = _git(call)
        reg = git.reader.pins.worktree(wplan["uid"], wplan["repo"])["reg"] if git is not None else None
        pin = ((reg or {}).get("pins") or {}).get("worktree")
        if reg is None or pin is None:
            raise HostError("worktree.unpinned", "the worktree was created but is not pinned")
        return {"path": reg["path"], "dev": pin["dev"], "ino": pin["ino"]}

    return await asyncio.wrap_future(call.service.submit(pinned))
