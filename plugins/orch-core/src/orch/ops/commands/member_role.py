"""orch member role: change a member's role"""

from orch.ops._dsl import STR, E, S, err, obj, operation

OP = operation(
    "member.role",
    "Human only",
    "Change a member's role.",
    who="human",
    props={
        "person": S("person id", pattern=r"^p_[0-9a-f]{32}$", **{"x-metavar": "PERSON"}),
        "role": E("role", "owner", "maintainer", "member", "viewer", **{"x-metavar": "ROLE"}),
    },
    required=("person", "role"),
    positional=("person", "role"),
    pre=("workspace_exists", "role_allows", "user_presence"),
    emits=("role.changed",),
    text="ok role.changed {person} {role} seq={seq}\nnext: {next}",
    data=obj({"person": STR, "role": STR}),
    errors=(err("role.denied"), err("not_found")),
)
