"""orch apply: apply an atomic batch"""

from orch.ops._dsl import FILE, INT, STR, arr, err, obj, operation

OP = operation(
    "apply",
    "Edit",
    "Apply a batch of operations atomically, from JSON (--file -).",
    who="agent",
    props={"file": FILE},
    required=("file",),
    pre=("ticket_exists", "base_rev_tracked", "grant_valid", "text_clean"),
    emits=(
        "ticket.updated",
        "log.added",
        "task.started",
        "task.done",
        "task.skipped",
        "task.blocked",
        "task.reopened",
        "artifact.added",
        "artifact.replaced",
        "question.asked",
    ),
    text="ok {key} apply {count} seq={seq}\nnext: {next}",
    data=obj({"count": INT, "events": arr(STR)}),
    errors=(
        err("parse.json"),
        err("conflict.section"),
        err("conflict.field"),
        err("transition.refused"),
        err("not_found"),
    ),
)
