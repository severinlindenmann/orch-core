"""orch list: list tickets"""

from orch.ops._dsl import INT, KEY, STR, B, E, I, S, arr, obj, operation

OP = operation(
    "list",
    "Read",
    "List tickets, newest first.",
    who="read",
    props={
        "status": E("only this status", "backlog", "open", "in_progress", "testing", "done", "closed"),
        "mine": B("only tickets you hold or are assigned"),
        "label": S("only this label"),
        "limit": I("at most this many", minimum=1, maximum=200, default=20, **{"x-metavar": "N"}),
    },
    pre=("workspace_exists",),
    text="ok list {count}\nnext: {next}",
    data=obj({"count": INT, "tickets": arr(obj({"key": KEY, "title": STR, "status": STR}))}),
)
