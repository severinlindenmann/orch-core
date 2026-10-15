"""orch member role: change a member's role"""

from typing import Any

from orch.ops._dsl import STR, E, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "member.role")
    person, role = args["person"], args["role"]
    h.who()
    if person not in h.store.state.workspace.members:
        raise OrchError("not_found", f"{person} is not a member")
    done = h.run({"type": "role.changed", "person": person, "role": role}, "workspace", f"make {person} {role}")
    return h.workspace_result(done, {"person": person, "role": role}, "orch member")


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
    handler=handle,
)
