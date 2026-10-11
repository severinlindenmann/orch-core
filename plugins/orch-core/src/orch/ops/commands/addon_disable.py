"""orch addon disable: disable an addon; its data stays"""

from typing import Any

from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "addon.disable")
    h.who()
    name = args["name"]
    if name not in h.store.state.workspace.addons:
        raise OrchError("not_found", "no such addon was granted", hint="orch addon list")
    a = h.store.state.workspace.addons[name]
    if not a["enabled"]:  # already disabled or purged: nothing to sign
        state = "purged" if a["purged"] else "disabled"
        return Result(
            data={"name": name},
            seq=h.store.head_seq("workspace"),
            lines=[f"already {state}: nothing signed"],
            hints=["orch addon list"],
        )
    done = h.run({"type": "addon.disabled", "name": name}, "workspace", "disable addon " + name)
    return h.workspace_result(done, {"name": name}, "orch addon list")


OP = operation(
    "addon.disable",
    "Admin",
    "Disable an addon; its data stays.",
    who="human",
    props={"name": S("addon name", **{"x-metavar": "NAME"})},
    required=("name",),
    positional=("name",),
    pre=("workspace_exists", "addon_exists", "role_allows", "user_presence"),
    emits=("addon.disabled",),
    text="ok addon.disabled {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle,
)
