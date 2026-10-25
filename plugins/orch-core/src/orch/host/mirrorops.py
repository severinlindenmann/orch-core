"""``mirror.sync {repo}`` (#1198 H1 L5, spec 13.2 item 5): bring the agent's read-only copy of a declared repository
up to date, so an agent that sees new commits of the person can ask for them (its clone fetches from the mirror).

Column P and G (kind ``pg_write``); N never. The mirror holds the repository's branches and tags only
(:mod:`orch.host.sharemirror`). It is the person's host that fetches, as the person; an agent only asks.

* **Limit.** Column G gets at most one sync per repository per 30 s: calls inside that time, and calls that arrive while
  a sync runs, are coalesced into it and get its answer (``synced: false``). The person is not limited but joins a
  running sync. No new error code: an answer is an answer, never ``rate.limited``.
* **Answer.** ``{repo, heads: {ref: id}, synced, truncated}``. Tier A (agents run as the person) has nothing to sync:
  ``op.unsupported``. A failed integrity check keeps the old mirror and answers ``observe.unavailable`` in plain words.
"""

from __future__ import annotations

import asyncio
import os
import pwd
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import sharemirror
from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["MIRROR_OPS", "execute", "serve"]

MIRROR_OPS: dict[str, int] = {"mirror.sync": 1}
_REPO = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9._-]{0,63}$"}
_SCHEMA = {
    "type": "object",
    "properties": {"repo": _REPO},
    "required": ["repo"],
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(_SCHEMA)
#: one gate for the host process: the key is (person, workspace, repository)
GATE = sharemirror.SyncGate()


def _check(call: Call) -> dict[str, Any]:
    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATOR.iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: {'.'.join(map(str, errors[0].absolute_path)) or 'args'} is not valid")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    return args


def _share(call: Call) -> sharemirror.Share:
    try:
        person = pwd.getpwuid(call.service.host_uid).pw_name
    except KeyError:
        person = ""
    share = sharemirror.share_for(person, call.service.state_dir)
    if share is None:
        raise HostError("op.unsupported", sharemirror.NOT_SHARED)
    return share


def serve(call: Call) -> dict[str, Any]:
    """The worker half: shape, the declared repository and its place. The fetch runs off the worker in
    :func:`execute`."""
    args = _check(call)
    service = call.service
    service.store.refresh()
    git = service.caps_for(call.principal).git
    if not git.enabled:
        raise HostError("observe.unavailable", "git cannot be read in this workspace now")
    declared = git.repo_path(service.store.layout, service.store.state.workspace.repos, args["repo"])
    if declared is None:
        raise HostError("not_found", f"{args['repo']} is not declared in settings.repos")
    return {"plan": {"repo": args["repo"], "path": os.fspath(declared), "share": _share(call)}}


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from .workspace import ok_result

    share: sharemirror.Share = plan["share"]
    ws8, name = call.service.workspace_id[:8], plan["repo"]
    try:
        # the person's call waits for a running sync; the agent's is also held to one per 30 s
        got, ran = GATE.run(
            f"{share.person}/{ws8}/{name}",
            lambda: sharemirror.sync_mirror(share, ws8, name, plan["path"]),
            limited=call.column.name == "G",
        )
    except sharemirror.ShareError as exc:
        text = str(exc)
        if "exists yet" in text:
            raise HostError("not_found", f"{name} is not shared with agents yet") from None
        raise HostError("observe.unavailable", text[:300]) from None
    return ok_result(
        {"repo": name, "heads": got.heads, "synced": bool(ran), "truncated": got.truncated},
    )
