"""orch member remove: remove a member"""

from orch.ops._dsl import STR, S, err, obj, operation

OP = operation(
    "member.remove",
    "Human only",
    "Remove a member; their claims are released.",
    who="human",
    props={"person": S("person id", pattern=r"^p_[0-9a-f]{32}$", **{"x-metavar": "PERSON"})},
    required=("person",),
    positional=("person",),
    pre=("workspace_exists", "role_allows", "user_presence"),
    emits=("member.removed",),
    text="ok member.removed {person} seq={seq}\nnext: {next}",
    data=obj({"person": STR}),
    errors=(err("role.denied"), err("not_found")),
)
