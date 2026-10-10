"""orch status: who you are, your grant, your cursor and what needs you"""

from orch.ops._dsl import INT, KEY, STR, err, obj, operation

OP = operation(
    "status",
    "Context",
    "Who you are, your grant, your cursor, your claim and what needs you.",
    who="read",
    pre=("workspace_exists",),
    text="ok status {person} cursor={cursor}[ grant={grant}][ claim={claim}]\nnext: {next}",
    data=obj(
        {"person": STR, "grant": STR, "claim": KEY, "cursor": INT, "new_events": INT},
        optional=("grant", "claim", "new_events"),
    ),
    errors=(err("not_found", "run orch init in a workspace", ["orch", "describe", "init"]),),
)
