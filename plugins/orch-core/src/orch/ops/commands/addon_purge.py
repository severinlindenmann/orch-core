"""orch addon purge: delete an addon's data"""

from orch.ops._dsl import STR, S, err, obj, operation

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
)
