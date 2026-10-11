"""orch addon purge: delete an addon's data"""

from typing import Any

from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "addon.purge")
    h.who()
    name = args["name"]
    if name not in h.store.state.workspace.addons:
        raise OrchError("not_found", "no such addon was granted", hint="orch addon list")
    done = h.run({"type": "addon.purged", "name": name}, "workspace", "purge addon " + name)
    return h.workspace_result(done, {"name": name}, "orch addon list")


OP = operation(
    "addon.purge",
    "Admin",
    "Delete an addon's data.",
    who="human",
    props={"name": S("addon name", **{"x-metavar": "NAME"})},
    required=("name",),
    positional=("name",),
    pre=("workspace_exists", "addon_exists", "role_allows", "user_presence"),
    emits=("addon.purged",),
    text="ok addon.purged {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle,
)
