"""orch addon list: list the addons"""

from typing import Any

from orch.addons import install
from orch.addons.manifest import ManifestError
from orch.addons.package import PackageError
from orch.ops._dsl import BOOL, INT, STR, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.runtime import Call


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Granted addons (with their state: active, disabled, purged, changed since the grant, missing) and packages that
    are installed but not granted. ``enabled`` is true only for an active addon."""
    c = Call.of(ctx, "addon.list")
    root = c.ws.require_root()
    view = c.store.state.workspace.addons
    registry = install.load_registry(root, view)
    granted = install.as_addons(view)
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    for name in sorted(granted):
        state = registry.state(name)
        rows.append({"name": name, "version": granted[name].version, "enabled": state == "active"})
        lines.append(f"{name} {granted[name].version} {state}")
    for name in install.installed_names(root):
        if name in granted:
            continue
        try:
            version = install.read_installed(root, name)[1].version
        except (PackageError, ManifestError):
            version, note = "?", "not granted, package invalid"
        else:
            note = "not granted"
        rows.append({"name": name, "version": version, "enabled": False})
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
    data=obj({"count": INT, "addons": arr(obj({"name": STR, "version": STR, "enabled": BOOL}))}),
    handler=handle,
)
