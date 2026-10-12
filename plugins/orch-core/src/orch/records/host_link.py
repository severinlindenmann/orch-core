"""The records tool's way to the host: the person's registry names the workspace's root and host; requests go on the
pinned workspace socket (:mod:`orch.hostclient`), without a grant (column P)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
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


def _record_root(root: Path) -> Path:
    return root if (root / "config.json").is_file() else root / "orchestrator"


def resolve(env: Mapping[str, str], workspace: str | None) -> Resolved:
    from orch import hostclient
    from orch.host.custody import read_json
    from orch.host.registry import registry_path
    from orch.ops.runtime import _config, find_workspace, state_dir

    state = state_dir(env)
    wid = workspace
    if wid is None:
        from orch.ops.caps import Files

        files = Files.direct()
        here = find_workspace(env, os.getcwd(), files)
        cfg = _config(here, files) if here is not None else None
        wid = ((cfg or {}).get("workspace") or {}).get("id")
    if not isinstance(wid, str) or not _WS.fullmatch(wid):
        raise RecordsError("records.unsupported", "no workspace: pass --workspace ID or run it in the workspace")
    doc = read_json(registry_path(state), os.getuid()) or {"workspaces": []}
    entry = next((e for e in doc.get("workspaces") or [] if e.get("workspace_id") == wid), None)
    if entry is None:
        raise RecordsError(
            "records.unsupported",
            "the host does not serve this workspace; direct-mode records are a later item (D-P2-38)",
        )
    host_uid = entry.get("host_uid")
    if type(host_uid) is not int:
        raise RecordsError("records.unsupported", "the host registry entry has no host UID")
    target = hostclient.Target(
        wid, host_uid, hostclient.workspace_socket(env, state, host_uid, wid), "person", state, entry
    )

    def link(op: str, args: dict[str, Any]) -> dict[str, Any]:
        return hostclient._request(target, op, args, auth=None)["data"]  # noqa: SLF001 - the CLI's one request path

    return Resolved(wid, _record_root(Path(str(entry.get("root")))), link, ValidatedTips(state / "records.json"))
