"""orch addon preview: what turning an addon on would sign (read only)"""

from typing import Any

from orch.addons import settings_needs
from orch.addons.manifest import ManifestError
from orch.addons.package import PackageError
from orch.ops._dsl import BOOL, STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The ``addon.granted`` body that ``orch addon grant NAME`` would sign now: from the installed package, or for a
    built-in addon that is not installed from the shipped bytes, in memory. Writes nothing, installs nothing. The
    dashboard puts this body into the signed ``addon_enable`` intent; the host compares it again before step 0."""
    c = Call.of(ctx, "addon.preview")
    c.ws.require_root()
    name = args["name"]
    try:
        grant = settings_needs.preview_grant(c.store.layout.store, name)
    except KeyError:
        raise OrchError("not_found", f"no package for the addon {name}", hint="install the addon first") from None
    except (PackageError, ManifestError) as e:
        raise OrchError("invalid.input", f"addon {name}: {e}") from None
    addons = c.store.state.workspace.addons
    needs = settings_needs.NEEDS_SETTINGS.get(name)
    data = {
        "name": name,
        "grant": grant,
        "needs": needs,
        "set_up": settings_needs.is_set(c.store.state.workspace.settings, name),
        "on": settings_needs.is_on(addons, name),
    }
    return Result(
        data=data, lines=[f"{name} {grant['version']} {grant['package_sha256']}"], hints=["orch addon grant " + name]
    )


OP = operation(
    "addon.preview",
    "Admin",
    "Show what turning an addon on would sign, without installing or granting it.",
    who="read",
    props={"name": S("addon name", **{"x-metavar": "NAME"})},
    required=("name",),
    positional=("name",),
    pre=("workspace_exists",),
    text="ok addon.preview {name}\nnext: {next}",
    data=obj(
        {"name": STR, "grant": {"type": "object"}, "needs": {"type": ["string", "null"]}, "set_up": BOOL, "on": BOOL}
    ),
    errors=(err("not_found"),),
    handler=handle,
)
