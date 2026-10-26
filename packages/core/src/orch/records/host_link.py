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
    #: the store directory (``<root>/.orch``): where the record files and the records repo live. ``root`` stays the
    #: project (the registry root, the auto entry's root). ``None``: the root.
    store: Path | None = None

    @property
    def record_root(self) -> Path:
        return self.store if self.store is not None else self.root


def _find_here(env: Mapping[str, str]) -> tuple[str | None, Path | None, Any]:
    """The workspace id here, its root and the :class:`~orch.ops.runtime.Workspace` that found it (the config as
    discovery read it, never again by path)."""
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace

    ws = Workspace(env, cwd=os.getcwd(), files=Files.direct())
    wid = ((ws.config or {}).get("workspace") or {}).get("id")
    return (wid if isinstance(wid, str) else None), ws.root, ws


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
    wid, here, here_ws = workspace, None, None
    if wid is None:
        wid, here, here_ws = _find_here(env)
    if not isinstance(wid, str) or not _WS.fullmatch(wid):
        raise RecordsError("records.unsupported", "no workspace: pass --workspace ID or run it in the workspace")
    tips = ValidatedTips(state / "records.json")
    doc = read_json(registry_path(state), os.getuid()) or {"workspaces": []}
    entry = next((e for e in doc.get("workspaces") or [] if e.get("workspace_id") == wid), None)
    if entry is None:  # direct mode: the workspace here (or the auto entry's root) must be this one
        from orch.ops.caps import Files

        from .direct import DirectLink

        ident = raw = None
        if root is None:
            found_wid, here, here_ws = _find_here(env) if here is None else (wid, here, here_ws)
            root = here if found_wid == wid else None
        if root is None:
            raise RecordsError(
                "records.unsupported", "no host serves this workspace and it is not the one here: run it there"
            )
        pin = None
        if here_ws is not None and root == here:  # found here: the config discovery read, the root's identity
            from orch.ops.errors import OrchError

            try:
                here_ws.verify_root()
            except OrchError as exc:
                raise RecordsError("records.unsupported", str(exc.message), exit_=3) from None
            cfg, ident, raw = here_ws.config, here_ws.ident, here_ws.config_bytes
            pin = here_ws.store_pin
        else:  # the auto entry's root: named, not discovered
            cfg = _config(Path(root), Files.direct())
        if ((cfg or {}).get("workspace") or {}).get("id") != wid:
            raise RecordsError("records.unsupported", "the directory is not this workspace")
        direct = DirectLink(Path(root), wid, state, ident=ident, config=raw, store_pin=pin)
        from orch import layout as layout_mod

        try:
            store_dir = here_ws.store_dir if here_ws is not None and root == here else layout_mod.store_of(root)
        except layout_mod.LayoutError as exc:
            raise RecordsError("records.unsupported", str(exc), exit_=3) from None
        return Resolved(wid, Path(root), direct, tips, state, "direct", direct.close, store_dir)
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

    base = Path(str(entry.get("root")))
    from orch.layout import STORE_DIR

    store = base / STORE_DIR
    from orch import layout as layout_mod

    try:
        on_disk = layout_mod.store_of(base)
    except layout_mod.LayoutError as exc:
        raise RecordsError("records.unsupported", str(exc), exit_=3) from None
    if on_disk != store:
        raise RecordsError(
            "records.unsupported", "the store on disk is not where the host registry says it is", exit_=3
        )
    return Resolved(wid, base, link, tips, state, "host", store=store)
