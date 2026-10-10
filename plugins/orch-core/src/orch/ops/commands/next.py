"""orch next: the ticket to work on next"""

from orch.ops._dsl import STR, obj, operation

OP = operation(
    "next",
    "Read",
    "The ticket to work on next.",
    who="read",
    pre=("workspace_exists",),
    text="ok next {target}\nnext: {next}",
    data=obj({"target": STR, "title": STR}, optional=("title",)),
)
