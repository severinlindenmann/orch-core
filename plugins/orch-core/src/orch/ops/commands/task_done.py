"""orch task done: finish a task, with its check and evidence"""

from orch.ops._dsl import MSG, STR, TASK, B, S, err, obj, operation

OP = operation(
    "task.done",
    "Edit",
    "Finish a task: --run runs its verify command and stores the receipt; prints the next task.",
    who="agent",
    props={
        "task": TASK(),
        "run": B("run the task's verify command first"),
        "artifact": S("evidence file to store", **{"x-metavar": "PATH"}),
        "ac": S("criterion the evidence is for", pattern=r"^AC[1-9][0-9]*$", **{"x-metavar": "AC"}),
        "message": MSG,
    },
    required=("task",),
    positional=("task",),
    pre=("ticket_exists", "session_holds_claim", "task_exists", "session_holds_lease", "grant_valid", "text_clean"),
    emits=("task.done", "artifact.added"),
    text="ok {key} task.done {task}[ receipt={receipt}][ artifact={artifact}] seq={seq}\nnext: {next}",
    data=obj({"task": STR, "receipt": STR, "artifact": STR}, optional=("receipt", "artifact")),
    errors=(
        err("verify.failed"),
        err("lease.required"),
        err("claim.required"),
        err("not_found"),
        err("transition.refused"),
    ),
)
