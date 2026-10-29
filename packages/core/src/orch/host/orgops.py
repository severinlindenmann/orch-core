"""Company skills and MCP servers, host side (M3 H-2; hub contract 7.3 and 7.4). The CLI is ``orch org ...``.

Ops:

* ``org.offers``: what the hub offers this workspace and where each item stands (``off``, ``on``, ``changed``,
  ``needs_isolation``): names, digests and status only. P and G (an agent may know what exists; it gets no text and no
  address).
* ``org.show {kind, name}``: the full skill text, or the exact address, sign-in, secret name and description, and the
  honest limits. P only, owner.
* ``org.accept {kind, name, digest}``: the owner turns an item on, for exactly the digest they were shown. P
  only, owner.
  Written to ``hub/org/accepted.json``: the same host op plus 0600 file as ``space.accept`` (a signed person op
  is a known
  gap, tracked in orch-core#1263).
* ``org.tool {server, tool, on, pin?}``: turn one tool of an accepted company server On (for exactly the pin shown;
  ``changed`` if it moved) or Off. P only, owner. Every tool starts Off.
* ``org.decline {kind, name}``: turn an item off again. P only, owner.
* ``org.sync``: fetch both bundles from the bound hub and verify them (no rollback). Grants nothing. P, and G at
  most once
  a minute.

Everything is Off until the owner accepts; an agent can never accept, decline or read the text (``op.not_allowed``)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["NETWORK_OPS", "ORG_OPS", "ORG_READS", "execute", "serve"]

ORG_OPS: dict[str, int] = {
    "org.offers": 1,
    "org.show": 1,
    "org.accept": 1,
    "org.decline": 1,
    "org.tool": 1,
    "org.sync": 1,
}
ORG_READS = frozenset({"org.offers"})
NETWORK_OPS = frozenset({"org.sync"})  # talks to the hub: off the workspace worker
AGENT_SYNC_MIN_S = 60.0
_KIND = {"enum": ["skill", "mcp"]}
_NAME = {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,39}$"}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "org.offers": {"type": "object", "properties": {}, "additionalProperties": False},
    "org.sync": {"type": "object", "properties": {}, "additionalProperties": False},
    "org.show": {
        "type": "object", "properties": {"kind": _KIND, "name": _NAME}, "required": ["kind", "name"],
        "additionalProperties": False,
    },
    "org.decline": {
        "type": "object", "properties": {"kind": _KIND, "name": _NAME}, "required": ["kind", "name"],
        "additionalProperties": False,
    },
    "org.tool": {
        "type": "object",
        "properties": {
            "server": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"},
            "tool": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
            "on": {"type": "boolean"},
            "pin": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        },
        "required": ["server", "tool", "on"],
        "additionalProperties": False,
    },
    "org.accept": {
        "type": "object",
        "properties": {"kind": _KIND, "name": _NAME, "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}},
        "required": ["kind", "name", "digest"],
        "additionalProperties": False,
    },
}  # fmt: skip
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
_MU = threading.Lock()
_LAST_AGENT_SYNC: dict[str, float] = {}


def _owner(call: Call) -> None:
    from .spaceops import _owner as space_owner

    space_owner(call)  # column P and the owner role; an agent is op.not_allowed


def _check(call: Call) -> tuple[str, dict[str, Any]]:
    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: {'.'.join(map(str, errors[0].absolute_path)) or 'args'} is not valid")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op not in ORG_READS:
        if service.state == "degraded":
            raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    if op in ("org.show", "org.accept", "org.decline", "org.tool"):
        _owner(call)  # the owner at the person column; never an agent
    elif op == "org.sync":
        if call.column.name == "G":
            _agent_rate(call)
        elif call.column.name != "P":
            raise HostError("op.not_allowed", "org.sync is for a person or an agent with a grant")
    elif call.column.name not in ("P", "G"):
        raise HostError("op.not_allowed", f"{op} is for a person or an agent with a grant")
    service.store.refresh()
    return op, args


def _agent_rate(call: Call) -> None:
    """An agent may ask for a sync (it grants nothing) but not in a loop: once a minute per workspace."""
    now = call.service.clock()
    with _MU:
        last = _LAST_AGENT_SYNC.get(call.service.workspace_id)
        if last is not None and now - last < AGENT_SYNC_MIN_S:
            raise HostError(
                "rate.limited",
                "org.sync: an agent may sync once a minute",
                retry_after_ms=int(1000 * (AGENT_SYNC_MIN_S - (now - last))),
            )
        _LAST_AGENT_SYNC[call.service.workspace_id] = now


def serve(call: Call) -> dict[str, Any]:
    """The worker half. Everything but ``org.sync`` is local files and finishes here; a sync is planned for
    :func:`execute`."""
    from . import orgstate
    from .workspace import ok_result

    op, args = _check(call)
    service = call.service
    if op == "org.sync":
        from . import peerops

        peerops._hub_state(service, full=False)  # noqa: SLF001 - a bound hub, either mode (else hub.not_joined)
        return {"plan": {"op": op, "args": args}}
    if op == "org.offers":
        return ok_result(orgstate.offers_view(service))
    if op == "org.show":
        return ok_result(orgstate.show(service, args["kind"], args["name"]))
    if op == "org.accept":
        return ok_result(orgstate.accept(service, args["kind"], args["name"], args["digest"], call.viewer or ""))
    if op == "org.tool":
        if args["on"] and "pin" not in args:
            raise HostError("invalid.input", "org.tool: pin is required to turn a tool on")
        got = orgstate.set_tool(service, args["server"], args["tool"], args["on"], args.get("pin"), call.viewer or "")
        return ok_result(got)
    return ok_result(orgstate.decline(service, args["kind"], args["name"]))  # org.decline


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_sync, call.service)


def _report_use(service: WorkspaceService, link: Any) -> None:
    """Counts only (``secret_use {secret_id, day, n}``). Best effort: a refusal or a broken link puts the counts
    back and never blocks or fails the sync."""
    from . import orgsecrets

    rows = orgsecrets.take_usage(service)
    for i, row in enumerate(rows):
        try:
            link.call("secret_use", row)
        except Exception:  # noqa: BLE001
            orgsecrets.restore_usage(service, rows[i:])
            return


def _sync(service: WorkspaceService) -> dict[str, Any]:
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.tlslink.client import LinkClosed, LinkError, LinkTimeout

    from . import hubops, orgstate, peerops
    from .workspace import ok_result

    bodies: dict[str, Any] = {}
    ops = [("skill", "org_skills_get"), ("mcp", "mcp_servers_get")]
    if orgstate.isolated(service):
        ops.append(("secrets", "org_secrets_get"))  # tier B only: tier A never asks for a wrap (condition 11)
    try:
        link = peerops._open_link(service)  # noqa: SLF001 - mode-agnostic, one short link for the requests
        try:
            for kind, op in ops:
                try:
                    bodies[kind] = link.call(op, {})
                except (LinkError, HubRefused) as exc:
                    bodies[kind] = exc.code  # a refusal (an older hub: unknown_op) is the result of that kind
            if "secrets" in bodies:
                _report_use(service, link)
        finally:
            link.close()
    except (OSError, LinkClosed, LinkTimeout):
        raise hubops._refused("hub_unreachable") from None  # noqa: SLF001 - nothing changed; the cache keeps working
    except HubRefused as exc:
        raise hubops._refused(exc.code) from None  # noqa: SLF001
    result = orgstate.sync_bundles(service, bodies)
    return ok_result({"result": result})
