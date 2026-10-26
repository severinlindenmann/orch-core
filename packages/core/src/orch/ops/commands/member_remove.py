"""orch member remove: remove a member"""

from typing import Any

from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "member.remove")
    person = args["person"]
    h.who()
    if person not in h.store.state.workspace.members:
        raise OrchError("not_found", f"{person} is not a member")
    done = h.run({"type": "member.removed", "person": person}, "workspace", f"remove member {person}")
    return h.workspace_result(done, {"person": person}, "orch member")


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
    handler=handle,
)
