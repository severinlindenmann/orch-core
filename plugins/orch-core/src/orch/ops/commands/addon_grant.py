"""orch addon grant: grant an addon its capabilities (owner only); also enables it"""

from orch.ops._dsl import STR, S, err, obj, operation

OP = operation(
    "addon.grant",
    "Admin",
    "Grant an addon its capabilities (owner only); also enables it.",
    who="human",
    props={"name": S("addon name", **{"x-metavar": "NAME"})},
    required=("name",),
    positional=("name",),
    pre=("workspace_exists", "addon_exists", "role_allows", "user_presence"),
    emits=("addon.granted",),
    text="ok addon.granted {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR}),
    errors=(err("role.denied"), err("not_found")),
)
