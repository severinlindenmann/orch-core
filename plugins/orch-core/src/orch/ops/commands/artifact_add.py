"""orch artifact add: store a file as evidence"""

from typing import Any

from orch.ops import plans
from orch.ops._dsl import INT, REF_PATTERN, STR, E, S, err, obj, operation
from orch.ops.base import Context, Result


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    return plans.run(ctx, "artifact.add", args, plans.artifact_add)


OP = operation(
    "artifact.add",
    "Edit",
    "Store a file; works without a grant (unattended, never evidence). --ac and --task need a grant.",
    who="unattended",
    props={
        "path": S("file to store", **{"x-metavar": "PATH"}),
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
        "kind": E(
            "what it is",
            "screenshot",
            "log",
            "report",
            "link",
            "dataset",
            "build",
            "diagram",
            "receipt",
            "other",
            default="other",
        ),
        "name": S("name in the ticket; default: the file name", **{"x-metavar": "NAME"}),
        "ac": S(
            "criterion it is evidence for (needs a grant)",
            pattern=r"^AC[1-9][0-9]*$",
            **{"x-metavar": "AC", "x-needs-grant": True},
        ),
        "task": S(
            "task it belongs to (needs a grant)",
            pattern=r"^T[1-9][0-9]*$",
            **{"x-metavar": "TASK", "x-needs-grant": True},
        ),
        "label": S("short label", **{"x-metavar": "TEXT"}),
    },
    required=("path",),
    positional=("path",),
    pre=(
        "ticket_exists",
        "ticket_visible",
        "ticket_open_for_work",
        "unattended_scope",
        "unattended_quota",
        "text_clean",
    ),
    emits=("artifact.added",),
    text="ok {key} artifact.added {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR, "sha256": STR, "bytes": INT}),
    errors=(err("quota.unattended"), err("transition.refused"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
