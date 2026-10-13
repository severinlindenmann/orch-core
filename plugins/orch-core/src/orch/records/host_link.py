"""The records tool's way to the host: the person's registry names the workspace's root and host; requests go on the
pinned workspace socket (:mod:`orch.hostclient`), without a grant (column P)."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import RecordsError
from .tool import Link, ValidatedTips

__all__ = ["TRUST_PROMPT", "Resolved", "resolve"]

TRUST_PROMPT = (
    "Mark {url} as a private records remote for this workspace.\n"
    "orch trusts your word that only you read this remote. If someone else can read it, they get this workspace's "
    "convenience-signed history. The mark covers this exact URL only: if the remote's URL, pushurl or an insteadOf "
    "rewrite changes, pushes are refused again until you mark the new URL."
)
_WS = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Resolved:
    workspace_id: str
    root: Path
    link: Link
    tips: ValidatedTips
    state: Path
    mode: str  # "host" | "direct"
    close: Callable[[], None] = lambda: None


def _find_here(env: Mapping[str, str]) -> tuple[str | None, Path | None]:
    from orch.ops.caps import Files
    from orch.ops.runtime import _config, find_workspace

    files = Files.direct()
    here = find_workspace(env, os.getcwd(), files)
    cfg = _config(here, files) if here is not None else None
    wid = ((cfg or {}).get("workspace") or {}).get("id")
    return (wid if isinstance(wid, str) else None), here


def resolve(
    env: Mapping[str, str],
    workspace: str | None,
    *,
    state: Path | None = None,
    root: Path | None = None,
    deadline: Callable[[], float] | None = None,
) -> Resolved:
    """Host mode when the person's own registry names the workspace (requests on its pinned socket, column P),
    else direct mode (D-P2-38) on the workspace found here, or at ``root`` (the auto entry of a direct workspace)."""
    from orch import hostclient
    from orch.host.custody import read_json
    from orch.host.registry import registry_path
    from orch.ops.runtime import _config, state_dir

    state = Path(state) if state is not None else state_dir(env)
    wid, here = workspace, None
    if wid is None:
        wid, here = _find_here(env)
    if not isinstance(wid, str) or not _WS.fullmatch(wid):
        raise RecordsError("records.unsupported", "no workspace: pass --workspace ID or run it in the workspace")
    tips = ValidatedTips(state / "records.json")
    doc = read_json(registry_path(state), os.getuid()) or {"workspaces": []}
    entry = next((e for e in doc.get("workspaces") or [] if e.get("workspace_id") == wid), None)
    if entry is None:  # direct mode: the workspace here (or the auto entry's root) must be this one
        from orch.ops.caps import Files

        from .direct import DirectLink

        if root is None:
            found_wid, here = _find_here(env) if here is None else (wid, here)
            root = here if found_wid == wid else None
        if root is None:
            raise RecordsError(
                "records.unsupported", "no host serves this workspace and it is not the one here: run it there"
            )
        cfg = _config(Path(root), Files.direct())
        if ((cfg or {}).get("workspace") or {}).get("id") != wid:
            raise RecordsError("records.unsupported", "the directory is not this workspace")
        direct = DirectLink(Path(root), wid, state)
        return Resolved(wid, Path(root), direct, tips, state, "direct", direct.close)
    host_uid = entry.get("host_uid")
    if type(host_uid) is not int:
        raise RecordsError("records.unsupported", "the host registry entry has no host UID")
    target = hostclient.Target(
        wid, host_uid, hostclient.workspace_socket(env, state, host_uid, wid), "person", state, entry
    )

    def link(op: str, args: dict[str, Any]) -> dict[str, Any]:
        from orch.host.control_client import new_idem

        from .tool import WRITE_OPS

        idem = new_idem() if op in WRITE_OPS else None  # the mark and the note are writes (an outcome record)
        from orch import records

        timeout = None
        if deadline is not None:  # read at each call: a human confirmation restarts the run's budget
            timeout = deadline() - records.MONOTONIC()
            if timeout <= 0.05:
                raise RecordsError("records.budget", "the records run used its 60 s budget; try again")
        try:
            return hostclient._request(target, op, args, auth=None, idem=idem, timeout=timeout)["data"]  # noqa: SLF001
        except hostclient.HostFailure as exc:
            raise RecordsError(exc.code, str(exc.message), hint=exc.hint, exit_=exc.wire_exit) from None

    return Resolved(wid, Path(str(entry.get("root"))), link, tips, state, "host")
