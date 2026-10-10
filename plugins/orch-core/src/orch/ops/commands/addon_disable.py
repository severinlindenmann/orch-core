"""orch addon disable: disable an addon; its data stays"""

from orch.ops._dsl import STR, S, err, obj, operation

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
)
