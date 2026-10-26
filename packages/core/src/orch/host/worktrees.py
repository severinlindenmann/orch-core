"""Worktrees (H11d, addon #308, C18, D-P2-32, §12.7): ``worktree.list``, ``worktree.create``, ``worktree.remove``.

One worktree per (ticket, linked repo), at the host-chosen path ``<declared repo path>.worktrees/<WS8>-<KEY>``
(``WS8``: the first 8 characters of the workspace id; keys can repeat across workspaces sharing a repo), on the
ticket's **current** ``links.branches.<repo>`` branch (none: ``worktree.no_branch``; the host never invents one).

* **The host never runs git** (D-P2-19, D-P2-24). The runner runs ``git worktree add|remove`` (:mod:`orch.runner.wtgit`:
  hooks, fsmonitor and network off) under the per-repo lock; the host then pins the new worktree through its reader
  (:meth:`orch.gitread.client.HostGitReader.pin_worktree`) and writes the registration with a new **registration
  generation** to ``git-pins.json`` (``worktrees.<ticket uid>.<repo>``) before anything uses it. A failed pin leaves an
  ``unpinned`` registration (Remove, then Create again), never a usable one.
* **Columns** (D-P2-26): P on a visible ticket (``force`` on remove is P only); G only on the ticket its session holds
  the live claim on (the claim's grant is the request's grant and ``auth.session`` is in the claim's session family),
  **rechecked under the per-repo lock right before the runner executes git**; N never.
* **Gate** (D-P2-31): dashboard execution on for the workspace on this machine and the ``worktrees`` addon granted,
  else ``runner.disabled`` (the message names what is missing first: addon, machine); no runner: ``runner.unavailable``.
* **Generations.** Create and remove each bump the generation of (ticket, repo); a request prepared against another
  generation, pin or working copy is ``observe.unavailable`` at binding and at precommit
  (:func:`orch.host.human.bound_copy`), never a fallback to the declared working copy.
* **Remove**: no registration is ``worktree.none``; a live run with that worktree as its cwd is ``worktree.in_use``;
  ``tree.check`` first, as a removal check (no ignore rule applies: an ignored file blocks too, #583 round 3), and
  dirty (or unknown) is ``worktree.dirty``, listing the files, unless ``force`` (P only); the branch is never
  deleted; the registration goes only after the runner reports success.
* **Observations.** ``worktree.list {check}`` reports what the reader saw in that directory at that moment (``head``,
  ``dirty``, ``state``); like every clean-tree result it is not proof of which bytes any test ran on.
"""

from __future__ import annotations

import asyncio
import collections
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["WORKTREE_CAPS", "WORKTREE_OPS", "WRITE_OPS", "execute", "serve"]

WORKTREE_OPS: dict[str, int] = {"worktree.list": 1, "worktree.create": 1, "worktree.remove": 1}
WRITE_OPS = frozenset({"worktree.create", "worktree.remove"})
WORKTREE_CAPS = ["worktree/1"]
ADDON = "worktrees"
#: §0.11 (v1.6): 200 registered per workspace; create and remove 10 per minute per person; 8 checks per list call
MAX_REGISTERED = 200
PER_MINUTE = 10
CHECKS_PER_LIST = 8
#: the runner's own limit is 30 s per git command; the host waits a little longer for its report
GIT_TIMEOUT = 40.0
LOCK_WAIT = 30.0
_REF = {"type": "string", "minLength": 1, "maxLength": 80}
_REPO = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9._-]{0,63}$"}
_IDEM = re.compile(r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SCHEMAS = {
    "worktree.list": {
        "type": "object",
        "properties": {"ticket": _REF, "check": {"type": "boolean"}},
        "additionalProperties": False,
    },
    "worktree.create": {
        "type": "object",
        "properties": {"ticket": _REF, "repo": _REPO, "base": {"enum": ["default", "current"]}},
        "required": ["ticket"],
        "additionalProperties": False,
    },
    "worktree.remove": {
        "type": "object",
        "properties": {"ticket": _REF, "repo": _REPO, "force": {"type": "boolean"}},
        "required": ["ticket"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
_RATE: dict[tuple[str, str], collections.deque[float]] = {}
_RATE_MU = threading.Lock()
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# -- the worker-thread phase ------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    """The worker half: ``worktree.list`` answers here; create and remove answer a plan that :func:`execute` runs on
    the event loop (the runner command is async), with every check repeated there under the per-repo lock."""
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    opts = call.frame.get("opts") or {}
    if opts.get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op in WRITE_OPS and service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    service.store.refresh()
    if op == "worktree.list":
        return ok_result({"worktrees": _list(call, args)})
    idem = opts.get("idem")
    if idem is not None and (not isinstance(idem, str) or not _IDEM.fullmatch(idem)):
        raise HostError("invalid.input", "opts.idem is not an idem")
    plan = _plan(call, op, args, idem)
    if plan.get("done") is not None:  # an idem retry of a create that already happened: the same object
        return ok_result({"worktree": plan["done"]})
    return {"plan": plan}


def _git(call: Call) -> Any:
    git = call.service.caps_for(call.principal).git
    if not git.enabled or git.reader is None:
        raise HostError("observe.unavailable", "git cannot be read in this workspace now")
    return git


def _ticket(call: Call, ref: str) -> Any:
    from .visibility import Viewer

    return Viewer.of(call).ticket(call.service.store, ref)


def _repo_of(view: Any, repo: str | None) -> str:
    linked = list((view.fields.get("links") or {}).get("repos") or [])
    if repo is None:
        if len(linked) != 1:
            raise HostError("invalid.input", "name the repo: the ticket links " + ("no repo" if not linked else
                                                                                   "several repos"))  # fmt: skip
        return linked[0]
    if repo not in linked:
        raise HostError("not_found", "no such repo on this ticket")
    return repo


def _claim_ok(call: Call, view: Any) -> bool:
    """G: the ticket's claim is live, held under this request's grant, by this session's family (D-P2-26)."""
    if call.column.name == "P":
        return True
    if call.column.name != "G":
        return False
    from orch.model.claims import in_family

    claim = view.claim
    grant = call.verified.grant if call.verified is not None else None
    session = (call.frame.get("auth") or {}).get("session")
    return (
        claim is not None
        and claim.live
        and grant is not None
        and claim.grant == grant.id
        and isinstance(session, str)
        and in_family(session, claim.session)
    )


def _gate(call: Call) -> None:
    service = call.service
    from . import hubpolicy

    if not hubpolicy.running(service, ADDON):  # granted, on, and not paused by the hub policy
        raise HostError(
            "runner.disabled",
            f"dashboard execution: the worktrees addon is not granted (missing: addon){hubpolicy.why(service, ADDON)}",
            detail={"missing": "addon"},
        )
    hub = getattr(service, "runners", None)
    if hub is None or not hub.runner_state(service.workspace_id)["enabled"]:
        raise HostError(
            "runner.disabled",
            "dashboard execution is off for this workspace on this machine (missing: machine); "
            "turn it on in your terminal: orch runner enable",
            detail={"missing": "machine"},
        )
    if not hub.connected:
        raise HostError("runner.unavailable", "no runner is connected", retry_after_ms=1000)
    hub.require_current()


def _rate(call: Call) -> None:
    key = (call.service.workspace_id, str(call.viewer))
    now = time.monotonic()
    with _RATE_MU:
        q = _RATE.setdefault(key, collections.deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PER_MINUTE:
            raise HostError("rate.limited", "at most 10 worktree creations and removals per minute", retry_after_ms=
                            int((60 - (now - q[0])) * 1000) + 1)  # fmt: skip
        q.append(now)


def worktree_path(declared: str, workspace_id: str, key: str) -> str:
    """§12.7: ``<declared repo path>.worktrees/<WS8>-<KEY>``, joined lexically from host-known values only."""
    return f"{declared.rstrip('/')}.worktrees/{workspace_id[:8]}-{key}"


def _plan(call: Call, op: str, args: dict[str, Any], idem: str | None) -> dict[str, Any]:
    """Every check of create or remove that does not need the runner; :func:`_recheck` repeats them under the lock."""
    service, store = call.service, call.service.store
    if op == "worktree.remove" and args.get("force") and call.column.name != "P":
        raise HostError("op.not_allowed", "only a person can force the removal of a worktree")
    view = _ticket(call, args["ticket"])
    if not _claim_ok(call, view):
        raise HostError("op.not_allowed", "an agent creates or removes worktrees only on the ticket its session claims")
    repo = _repo_of(view, args.get("repo"))
    _gate(call)
    git = _git(call)
    slot = git.reader.pins.worktree(view.uid, repo)
    reg = slot["reg"]
    plan: dict[str, Any] = {
        "op": op, "uid": view.uid, "key": view.key, "repo": repo, "gen": slot["gen"],
        "force": bool(args.get("force")), "base": args.get("base", "default"), "idem": idem,
    }  # fmt: skip
    if op == "worktree.create":
        if reg is not None:
            if idem is not None and reg.get("idem") == idem:
                return {**plan, "done": _row(view, repo, slot)}
            raise HostError("worktree.exists", f"{view.key} already has a worktree for {repo}")
        branch = ((view.fields.get("links") or {}).get("branches") or {}).get(repo)
        if not branch:
            raise HostError("worktree.no_branch", f"{view.key} links no branch for {repo}; set links.branches first")
        declared = git.repo_path(store.layout, store.state.workspace.repos, repo)
        if declared is None:
            raise HostError("not_found", f"{repo} is not declared in settings.repos")
        dpins = git.reader.pins.get(os.fspath(declared))
        if dpins is None:
            raise HostError("observe.unavailable", f"{repo}: the declared repository is not pinned")
        if len(git.reader.pins.worktrees()) >= MAX_REGISTERED:
            raise HostError("quota.host", f"at most {MAX_REGISTERED} worktrees are registered per workspace")
        for uid, r, other in git.reader.pins.worktrees():  # one ticket per branch (git refuses it too)
            if r == repo and other["reg"]["branch"] == branch and uid != view.uid:
                raise HostError("worktree.branch_in_use", f"{branch} is checked out in another ticket's worktree")
        path = worktree_path(os.fspath(declared), service.workspace_id, view.key)
        declared_paths = {
            os.fspath(p) for n in store.state.workspace.repos
            if (p := git.repo_path(store.layout, store.state.workspace.repos, n)) is not None
        }  # fmt: skip
        if path in declared_paths:
            raise HostError("worktree.exists", "the worktree path is a declared repository")
        plan.update(branch=branch, path=path, declared=os.fspath(declared), repo_pin=dpins.worktree.to_json())
        plan["base_sha"] = _base(call, git, os.fspath(declared), repo, branch, plan["base"])
        return plan
    if reg is None:
        raise HostError("worktree.none", f"{view.key} has no worktree for {repo}")
    declared = git.repo_path(store.layout, store.state.workspace.repos, repo)
    dpins = git.reader.pins.get(os.fspath(declared)) if declared is not None else None
    if dpins is None:
        raise HostError("observe.unavailable", f"{repo}: the declared repository is not pinned")
    plan.update(branch=reg["branch"], path=reg["path"], declared=os.fspath(declared), repo_pin=dpins.worktree.to_json())
    return plan


def _base(call: Call, git: Any, declared: str, repo: str, branch: str, base: str) -> str | None:
    """The commit a new branch starts at, or ``None`` when the branch exists (``base`` applies only when git must
    create it). Read through the host's reader, so git gets a commit id, never a name it could interpret."""
    from orch.gitread.errors import Unavailable

    spec = call.service.store.state.workspace.repos.get(repo) or {}
    default = spec.get("default_branch") or "main"
    refs = [f"refs/heads/{branch}", f"refs/heads/{default}"]
    try:
        got = git.reader.observe(declared, refs, principal=git.principal)
    except Unavailable as exc:
        raise HostError("observe.unavailable", f"{repo}: {exc.reason}"[:300]) from None
    if got["refs"].get(refs[0]) is not None:
        return None
    sha = got["head_sha"] if base == "current" else got["refs"].get(refs[1])
    if sha is None:
        raise HostError("worktree.no_branch", f"{branch} does not exist and there is no {base} commit to start it at")
    return sha


def _recheck(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    """Under the per-repo lock, right before the runner runs git: authorization from a fresh snapshot, the claim (G),
    the gate, the registration still at the planned generation, and for remove the in-use and clean-tree checks."""
    call.authorize()
    call.service.store.refresh()
    view = _ticket(call, plan["uid"])
    if not _claim_ok(call, view):
        raise HostError("op.not_allowed", "an agent creates or removes worktrees only on the ticket its session claims")
    _gate(call)
    git = _git(call)
    slot = git.reader.pins.worktree(plan["uid"], plan["repo"])
    if slot["gen"] != plan["gen"]:
        raise HostError("worktree.exists" if plan["op"] == "worktree.create" else "worktree.none",
                        "the worktree registration changed meanwhile; look again")  # fmt: skip
    if plan["op"] == "worktree.create":
        branch = ((view.fields.get("links") or {}).get("branches") or {}).get(plan["repo"])
        if branch != plan["branch"]:
            raise HostError("worktree.no_branch", "the ticket's branch changed meanwhile; look again")
        return plan
    hub = call.service.runners
    for rec in hub.store.records.values():
        cwd = rec.get("cwd") or {}
        if (
            rec.get("workspace_id") == call.service.workspace_id
            and rec.get("state") in ("starting", "running")
            and cwd.get("kind") == "worktree"
            and cwd.get("repo") == plan["repo"]
            and cwd.get("ticket") in (plan["key"], plan["uid"])
        ):
            raise HostError("worktree.in_use", f"run {rec.get('run')} works in this worktree; stop it first")
    reg = slot["reg"]
    if not os.path.lexists(reg["path"]):
        return plan  # missing: only the registration (and git's record) is left to remove
    if not plan["force"]:
        # #583 round 3: removal never depends on ignore rules. Every file that is not tracked-and-unchanged blocks
        # it, ignored or not (node_modules, .venv, build output), listed with sizes; git's remove runs --force next.
        got = git.tree_check(reg["path"], plan["repo"], removal=True) if reg["pins"] is not None else None
        if got is None:
            raise HostError("worktree.dirty", "the worktree cannot be checked; remove with force (a person only)")
        if got["changed"] or got["more"]:
            from orch.gitread.treejob import render

            raise HostError("worktree.dirty", ("the worktree has files that are not committed (ignored files count "
                            "too): " + "; ".join(render(got, 10).splitlines()) + ". Commit or delete them, or remove "
                            "with force (a person only)")[:600])  # fmt: skip
    return plan


def _register(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    """After the runner created the worktree: pin it through the reader and write the registration with a new
    generation. A failed pin writes an ``unpinned`` registration and answers ``worktree.unpinned``."""
    from orch.gitread.errors import Unavailable

    git = _git(call)
    reason = None
    try:
        pins = git.reader.pin_worktree(plan["declared"], plan["path"], plan["branch"], principal=git.principal)
        pinned = pins.to_json()
    except Unavailable as exc:
        pinned, reason = None, exc.reason
    actor: dict[str, Any] = {"kind": "person" if call.column.name == "P" else "agent", "id": str(call.viewer)}
    if call.column.name == "G":
        actor["session"] = (call.frame.get("auth") or {}).get("session")
    reg = {"path": plan["path"], "branch": plan["branch"], "pins": pinned, "created_at": _iso(call.service.clock()),
           "created_by": actor, "idem": plan.get("idem")}  # fmt: skip
    try:
        git.reader.pins.set_worktree(plan["uid"], plan["repo"], reg, expect=plan["gen"])
    except (ValueError, OSError) as exc:
        raise HostError("worktree.unpinned", f"the worktree was created but could not be registered ({exc})") from None
    _audit(call, "worktree.create", plan, pinned=pinned is not None)
    if reason is not None:
        raise HostError("worktree.unpinned", f"the worktree was created but could not be pinned: {reason}"[:400])
    view = _ticket(call, plan["uid"])
    from .workspace import ok_result

    return ok_result({"worktree": _row(view, plan["repo"], git.reader.pins.worktree(plan["uid"], plan["repo"]))})


def _unregister(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from .workspace import ok_result

    git = _git(call)
    try:
        git.reader.pins.set_worktree(plan["uid"], plan["repo"], None, expect=plan["gen"])
    except (ValueError, OSError) as exc:
        raise HostError("internal", f"the worktree was removed but its registration could not be dropped ({exc})"
                        ) from None  # fmt: skip
    _audit(call, "worktree.remove", plan, force=plan["force"])
    return ok_result({"removed": True})


def _audit(call: Call, event: str, plan: dict[str, Any], **more: Any) -> None:
    from . import securitylog

    service = call.service
    securitylog.record(service.state_dir, service.host_uid, service.clock(), event, workspace_id=service.workspace_id,
                       ticket=plan["key"], repo=plan["repo"], branch=plan["branch"], principal=call.principal,
                       **more)  # fmt: skip


# -- the event-loop phase ---------------------------------------------------------------------------------------------


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    """Create or remove under the per-repo lock: recheck (worker), the runner's git, then register or unregister
    (worker). A runner that does not answer leaves the registration as it was (``outcome.unknown``)."""
    if "plan" not in planned:
        return planned  # an idem replay answered by the worker
    plan = planned["plan"]
    service = call.service
    key = (service.workspace_id, plan["declared"])
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), LOCK_WAIT)
    except TimeoutError:
        raise HostError("lock.busy", "another worktree command runs on this repository", retry_after_ms=1000) from None
    try:
        await asyncio.wrap_future(service.submit(_recheck_and_rate, call, plan))
        hub = service.runners
        args = {"op": "worktree.add" if plan["op"] == "worktree.create" else "worktree.remove",
                "repo_pin": plan["repo_pin"], "path": plan["path"], "branch": plan["branch"],
                "base": plan.get("base_sha"), "force": plan["force"], "generation": plan["gen"] + 1}  # fmt: skip
        hub.require_current()  # a runner of another release applies its old rules to this command too
        await hub.command("git", args, timeout=GIT_TIMEOUT)
        after = _register if plan["op"] == "worktree.create" else _unregister
        return await asyncio.wrap_future(service.submit(after, call, plan))
    finally:
        lock.release()


def _recheck_and_rate(call: Call, plan: dict[str, Any]) -> None:
    _recheck(call, plan)
    _rate(call)


# -- worktree.list ----------------------------------------------------------------------------------------------------


def _row(view: Any, repo: str, slot: dict[str, Any], observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """§12.7 ``Worktree``. Without a check: ``head``, ``dirty`` and ``checked_at`` are null and ``state`` is what the
    registration says (``unpinned``, ``stale_branch`` when the ticket was relinked, else ``ok``)."""
    reg = slot["reg"]
    current = ((view.fields.get("links") or {}).get("branches") or {}).get(repo)
    state = "unpinned" if reg["pins"] is None else ("stale_branch" if current != reg["branch"] else "ok")
    row = {"ticket": view.key, "repo": repo, "branch": reg["branch"], "path": reg["path"], "generation": slot["gen"],
           "created_at": reg["created_at"], "created_by": reg["created_by"], "head": None, "state": state,
           "dirty": None, "checked_at": None}  # fmt: skip
    if observed:
        row.update(observed)
    return row


def _observe(call: Call, git: Any, view: Any, repo: str, slot: dict[str, Any]) -> dict[str, Any]:
    """One check (``worktree.list {check}``): what the reader sees in that directory now, an observation only."""
    from orch.gitread.errors import Unavailable

    reg = slot["reg"]
    out: dict[str, Any] = {"checked_at": _iso(call.service.clock())}
    if reg["pins"] is None:
        return out
    pin = reg["pins"]["worktree"]
    try:
        st = os.lstat(reg["path"])
    except FileNotFoundError:
        return {**out, "state": "missing"}
    except OSError:
        return {**out, "state": "moved"}
    if (st.st_dev, st.st_ino) != (pin["dev"], pin["ino"]):
        return {**out, "state": "moved"}
    try:
        git.reader.verify_worktree(reg["path"], principal=git.principal)  # the .git file is still the pinned one
        got = git.reader.observe(reg["path"], [], principal=git.principal)
    except Unavailable:
        return {**out, "state": "moved"}
    out["head"] = got.get("head_sha")
    current = ((view.fields.get("links") or {}).get("branches") or {}).get(repo)
    if got.get("head_ref") != f"refs/heads/{reg['branch']}" or current != reg["branch"]:
        out["state"] = "stale_branch"
    tree = git.tree_check(reg["path"], repo)
    out["dirty"] = None if tree is None else (len(tree["changed"]) + (1 if tree["more"] else 0))
    return out


def _list(call: Call, args: dict[str, Any]) -> list[dict[str, Any]]:
    from .visibility import Viewer

    git = _git(call)
    store = call.service.store
    viewer = Viewer.of(call)
    wanted = viewer.ticket(store, args["ticket"]).uid if args.get("ticket") else None
    rows: list[dict[str, Any]] = []
    checks = 0
    for uid, repo, slot in git.reader.pins.worktrees():
        if wanted is not None and uid != wanted:
            continue
        view = store.ticket(uid)
        if view is None or not viewer.sees(view):  # the grant's scope for G, visibility for everyone (F6)
            continue
        observed = None
        if args.get("check") and checks < CHECKS_PER_LIST:
            checks += 1
            observed = _observe(call, git, view, repo, slot)
        rows.append(_row(view, repo, slot, observed))
        if len(rows) >= MAX_REGISTERED:
            break
    return rows
