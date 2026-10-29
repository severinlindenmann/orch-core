"""Glue between the host and the company MCP proxy (M3 H-4, T5).

One :class:`~orch.host.mcpproxy.Proxy` per :class:`~orch.host.runners.RunnerHub`, started lazily by the first launch
that has accepted servers. A launch mints a capability for the run (bound to workspace, binding_seq, run and server
set) and hands ``{port, capability, servers}`` to the runner; a run end, an unbind or leave, or a host stop revokes it.
Capabilities live in memory only, so a host restart drops them all (Opus host condition 4).

Condition 3: in tier A the host never fetches or unseals an org secret. :func:`org_secret` is the only secret lookup
and it answers ``None`` until the secrets slice (#1431-#1433) exists; the proxy also refuses a ``hub_secret`` server
before it asks while the workspace is not isolated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import mcpshared, orgstate

if TYPE_CHECKING:
    from .mcpproxy import Proxy
    from .runners import RunnerHub
    from .service import WorkspaceService

__all__ = ["launch_mcp", "org_secret", "proxy_for", "revoke_run", "revoke_workspace", "shutdown"]

LOG = logging.getLogger("orch.host.orgmcp")
PLACEHOLDER = "@orch-mcp"
FLAG = "--mcp-config"
_CLOSING: set[asyncio.Task] = set()


def org_secret(service: Any, name: str) -> str | None:
    """The org secret ``name`` for this workspace. Not built (decision D-d): always None, so a ``hub_secret`` server
    answers "unavailable" at call time. Tier A never gets here (condition 3)."""
    return None


def _tool_policy(service: Any, server: str) -> dict[str, str]:
    fn = getattr(orgstate, "tool_policy", None)  # arrives with the org.tool slice; none yet means nothing is On
    return fn(service, server) if fn is not None else {}


def _record_seen(service: Any, server: str, digest: str, tools: list[dict]) -> None:
    fn = getattr(orgstate, "record_seen", None)
    if fn is not None:
        fn(service, server, digest, tools)


def proxy_for(hub: RunnerHub, *, _upstream: Callable[[dict, str | None], Any] | None = None) -> Proxy:
    """The hub's proxy, made on first use. ``_upstream`` is a test seam for the very first call only."""
    existing = getattr(hub, "org_mcp", None)
    if existing is not None:
        return existing
    from .mcpproxy import Proxy
    from .mcpupstream import Upstream

    services: dict[str, WorkspaceService] = {}

    def svc(ws: str) -> Any:
        return services.get(ws)

    def served(ws: str) -> list[dict]:
        s = svc(ws)
        return [] if s is None else list(orgstate.served(s)["mcp"])

    def policy(ws: str, server: str) -> dict[str, str]:
        s = svc(ws)
        return {} if s is None else _tool_policy(s, server)

    def isolated(ws: str) -> bool:
        s = svc(ws)
        return False if s is None else orgstate.isolated(s)

    def secret(ws: str, name: str) -> str | None:
        s = svc(ws)
        return None if s is None else org_secret(s, name)

    def seen(ws: str, server: str, digest: str, tools: list[dict]) -> None:
        s = svc(ws)
        if s is not None:
            _record_seen(s, server, digest, tools)

    proxy = Proxy(
        upstream=_upstream or (lambda rec, bearer: Upstream(rec["url"], bearer)),
        served=served,
        tool_policy=policy,
        isolated=isolated,
        secret=secret,
        on_seen=seen,
    )
    hub.org_mcp = proxy  # type: ignore[attr-defined]
    hub.org_mcp_services = services  # type: ignore[attr-defined]
    return proxy


def _placeholder_ok(argv: list[str]) -> bool:
    at = argv.index(PLACEHOLDER) if argv.count(PLACEHOLDER) == 1 else -1
    return at > 0 and argv[at - 1] == FLAG


def _served_now(service: Any) -> tuple[list[dict], int] | None:
    try:
        records = list(orgstate.served(service)["mcp"])
        if not records:
            return None
        return records, int(orgstate._state(service).binding_seq)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - the agent starts without company servers rather than not at all
        return None


async def launch_mcp(call: Any, hub: RunnerHub, run: str, argv: list[str]) -> dict | None:
    """The spawn payload's ``org_mcp`` for this launch, or None. Only a claude-code ticket work folder agent has the
    placeholder (Opus condition 7); every doubt is None."""
    if not _placeholder_ok(argv):
        return None
    service = call.service
    got = await asyncio.to_thread(_served_now, service)
    if got is None:
        return None
    records, seq = got
    keep = []
    for rec in records:
        if mcpshared.server_name_ok(rec.get("name")):
            keep.append(rec)
        else:
            LOG.warning("a company MCP server with an unusable name was left out of a launch (%r)", rec.get("name"))
    if not keep:
        return None
    try:
        proxy = proxy_for(hub)
        port = await proxy.start()
        hub.org_mcp_services[service.workspace_id] = service  # type: ignore[attr-defined]
        cap = proxy.mint(service.workspace_id, seq, run, keep)
    except Exception:  # noqa: BLE001
        LOG.warning("the company MCP proxy could not start; the agent starts without it")
        return None
    return {"port": port, "capability": cap, "servers": [r["name"] for r in keep]}


def revoke_run(hub: RunnerHub, run: str) -> None:
    proxy = getattr(hub, "org_mcp", None)
    if proxy is not None:
        proxy.revoke_run(run)


def revoke_workspace(hub_or_service: Any, ws: str) -> None:
    hub = getattr(hub_or_service, "runners", hub_or_service)
    proxy = getattr(hub, "org_mcp", None)
    if proxy is not None:
        proxy.revoke_workspace(ws)


def shutdown(hub: RunnerHub) -> None:
    """Host stop: close the listener and drop every capability (they are memory-only anyway)."""
    proxy = getattr(hub, "org_mcp", None)
    if proxy is None:
        return
    hub.org_mcp = None  # type: ignore[attr-defined]
    try:
        task = asyncio.get_running_loop().create_task(proxy.close())
    except RuntimeError:
        return
    _CLOSING.add(task)
    task.add_done_callback(_CLOSING.discard)
