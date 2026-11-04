"""The MCP config file of a Claude Code agent in a ticket work folder (M3 H-4, company MCP proxy).

Such an agent is launched with ``--strict-mcp-config --mcp-config @orch-mcp`` (:func:`orch.host.launch.launch_argv`):
its own project ``.mcp.json`` never loads. The host puts the placeholder :data:`PLACEHOLDER` after :data:`FLAG`, and
the runner (under the person's uid) writes the file and swaps in its path, like the rules file (:mod:`.rules`).

* **Only the local proxy.** Each company MCP server is listed as ``http://127.0.0.1:<port>/m/<name>`` with the run's
  capability as bearer. The file holds no upstream address and no company secret: the host's proxy keeps those.
* **Not in the work folder.** ``<state>/runner/agent-mcp/<run>.json``, 0600, folder 0700, removed when the run exits.
* **Always written.** Without ``org_mcp`` the file has an empty ``mcpServers``.
* **Strict placeholder.** Once, directly after the flag, and only with an absolute ``ORCH_WORKSPACE``.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch.host.mcpshared import CAPABILITY_SHAPE, server_name_ok

from .supervise import SpawnError

__all__ = ["FLAG", "PLACEHOLDER", "SERVERS_LIMIT", "prepare", "problem", "remove"]

FLAG = "--mcp-config"
PLACEHOLDER = "@orch-mcp"
SERVERS_LIMIT = 20


def problem(raw: object) -> str | None:
    """None when ``raw`` (the spawn's ``org_mcp``) is acceptable or absent, else a short reason."""
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != {"port", "capability", "servers"}:
        return "shape"
    port, cap, servers = raw["port"], raw["capability"], raw["servers"]
    if type(port) is not int or not 1 <= port <= 65535:
        return "port"
    if type(cap) is not str or CAPABILITY_SHAPE.fullmatch(cap) is None:
        return "capability"
    if type(servers) is not list or not 1 <= len(servers) <= SERVERS_LIMIT:
        return "servers"
    if len(set(servers)) != len(servers) or not all(server_name_ok(n) for n in servers):
        return "servers"
    return None


def _dir(base: Path) -> Path:
    d = base / "agent-mcp"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SpawnError("run.spawn_failed", "the runner's MCP config folder is not usable")
    os.chmod(d, 0o700)
    return d


def _content(org_mcp: Mapping[str, Any] | None) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    if org_mcp is not None:
        for name in org_mcp["servers"]:
            servers[name] = {
                "type": "http",
                "url": f"http://127.0.0.1:{org_mcp['port']}/m/{name}",
                "headers": {"Authorization": f"Bearer {org_mcp['capability']}"},
            }
    return {"mcpServers": servers}


def prepare(
    base: Path, run: str, argv: list[str], env: Mapping[str, str], org_mcp: Mapping[str, Any] | None = None
) -> list[str]:
    """``argv`` with the placeholder replaced by the path of the config file written for ``run``; ``argv`` itself when
    it has none. Raises :class:`SpawnError` for a misplaced placeholder, ``org_mcp`` without a placeholder, or a file
    that cannot be written (the run then does not start)."""
    bad = problem(org_mcp)
    if bad is not None:
        raise SpawnError("invalid.input", f"spawn: org_mcp is not acceptable ({bad})")
    if PLACEHOLDER not in argv:
        if org_mcp is not None:
            raise SpawnError("invalid.input", "spawn: company MCP servers need the MCP config placeholder")
        return argv
    at = argv.index(PLACEHOLDER)
    if argv.count(PLACEHOLDER) != 1 or at == 0 or argv[at - 1] != FLAG:
        raise SpawnError("invalid.input", "spawn: the MCP config placeholder is misplaced")
    root = env.get("ORCH_WORKSPACE") or ""
    if not root.startswith("/"):
        raise SpawnError("invalid.input", "spawn: the MCP config needs the workspace")
    try:
        path = _dir(base) / f"{run}.json"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_content(org_mcp), f)
    except OSError:
        raise SpawnError("run.spawn_failed", "the MCP config file for the run could not be written") from None
    return [*argv[:at], str(path), *argv[at + 1 :]]


def remove(base: Path, run: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(base / "agent-mcp" / f"{run}.json")
