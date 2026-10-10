"""orch artifact list: list the stored files"""

from orch.ops._dsl import INT, REF, STR, arr, err, obj, operation

OP = operation(
    "artifact.list",
    "Edit",
    "List the ticket's artifacts.",
    who="read",
    props={"ref": REF()},
    positional=("ref",),
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} artifact.list {count}\nnext: {next}",
    data=obj({"count": INT, "artifacts": arr(obj({"name": STR, "kind": STR, "ac": STR}, optional=("ac",)))}),
    errors=(err("not_found"), err("ambiguous_ref")),
)
