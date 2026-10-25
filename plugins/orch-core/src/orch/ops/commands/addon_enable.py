"""orch addon enable: turn a granted, disabled addon on (owner only); no second permissions review"""

from typing import Any

from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The event names the version and the package digest of the CURRENT signed grant: it turns on exactly what the
    owner approved at install (the model refuses any other pair), so there is no capability review here."""
    h = Human(ctx, "addon.enable")
    h.who()
    name = args["name"]
    h.call.ws.require_root()
    addons = h.store.state.workspace.addons
    if name not in addons:
        raise OrchError("not_found", "no such addon was granted", hint="orch addon list")
    a = addons[name]
    if a["purged"]:
        raise OrchError("invalid.input", "this addon was purged: install it again", hint="orch addon install ID")
    if a["enabled"]:
        return Result(
            data={"name": name},
            seq=h.store.head_seq("workspace"),
            lines=["already on: nothing signed"],
            hints=["orch addon list"],
        )
    from orch.addons import settings_needs

    refusal = settings_needs.grant_refusal(h.store.state.workspace.settings, name)  # AE4: never on without settings
    if refusal is not None:
        raise OrchError("invalid.input", refusal, hint="set it up in Settings, Add-ons, then turn it on")
    event = {"type": "addon.enabled", "name": name, "version": a["version"], "package_sha256": a["package_sha256"]}
    done = h.run(event, "workspace", f"enable addon {name} {a['version']}")
    return h.workspace_result(done, {"name": name}, "orch addon list")


OP = operation(
    "addon.enable",
    "Admin",
    "Turn on an installed addon that is Off (owner only); it gets the permissions you approved at install.",
    who="human",
    props={"name": S("addon name", **{"x-metavar": "NAME"})},
    required=("name",),
    positional=("name",),
    pre=("workspace_exists", "addon_exists", "role_allows", "user_presence"),
    emits=("addon.enabled",),
    text="ok addon.enabled {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle,
)
