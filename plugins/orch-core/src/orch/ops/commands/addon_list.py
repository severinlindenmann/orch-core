"""orch addon list: list the addons"""

import os
from typing import Any

from orch.addons import credential, install
from orch.addons.manifest import ManifestError
from orch.addons.package import PackageError
from orch.host.errors import HostError
from orch.ops._dsl import BOOL, INT, STR, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call


def _credential(c: Call, name: str, state: str, capabilities: Any) -> str | None:
    """``"stored"``, ``"missing"`` or ``"unsafe"`` (the stored file or its directories fail the custody check) for an
    active add-on whose grant holds ``credential``: existence only, the value is never read into the answer.
    Absent for every other add-on (and for a store without a host state
    directory)."""
    state_dir = getattr(c.store, "host_state_dir", None)
    if state_dir is None or state != "active" or "credential" not in capabilities:
        return None
    try:
        return credential.status(state_dir, c.store.workspace_id, os.getuid(), name)
    except HostError:
        return "unsafe"


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Granted addons (with their state: active, disabled, purged, changed since the grant, missing) and packages that
    are installed but not granted (``not_granted``). ``enabled`` is true only for an active addon. A row of an
    active addon that holds ``credential``
    also has ``credential``: ``stored``, ``missing`` or ``unsafe``. P1: this is the one place that loads the
    registry; P2 loads it for every call that runs or writes an addon."""
    c = Call.of(ctx, "addon.list")
    c.ws.require_root()
    root = c.store.layout.store  # addons/ lives in the store
    view = c.store.state.workspace.addons
    registry = install.load_registry(root, view)
    granted = install.as_addons(view)
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    for name in sorted(granted):
        state = registry.state(name)
        row: dict[str, Any] = {
            "name": name, "version": granted[name].version, "enabled": state == "active", "state": state,
        }  # fmt: skip
        if state == "active":
            row["capabilities"] = sorted(granted[name].capabilities)  # the grant's, for a page that offers per capability
        held = _credential(c, name, state, granted[name].capabilities)
        if held is not None:
            row["credential"] = held
        rows.append(row)
        note = " since the grant: orch addon grant " + name if state == "changed" else ""
        lines.append(f"{name} {granted[name].version} {state}{note}")
    for name in install.installed_names(root):
        if name in granted:
            continue
        try:
            version = install.read_installed(root, name)[1].version
        except (PackageError, ManifestError):
            version, note = "?", "not granted, package invalid"
        else:
            note = "not granted"
        rows.append({"name": name, "version": version, "enabled": False, "state": "not_granted"})
        lines.append(f"{name} {version} {note}")
    hint = "orch addon grant NAME" if any(not r["enabled"] for r in rows) else "orch status"
    return Result(data={"count": len(rows), "addons": rows}, lines=lines, hints=[hint])


OP = operation(
    "addon.list",
    "Admin",
    "List the installed addons and whether each is enabled.",
    who="read",
    pre=("workspace_exists",),
    text="ok addon.list {count}\nnext: {next}",
    data=obj(
        {
            "count": INT,
            "addons": arr(
                obj(
                    {
                        "name": STR, "version": STR, "enabled": BOOL, "state": STR, "credential": STR,
                        "capabilities": arr(STR),
                    },
                    optional=("credential", "capabilities"),
                )
            ),
        }
    ),
    handler=handle,
)
