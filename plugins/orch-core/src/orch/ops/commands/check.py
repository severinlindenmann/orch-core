"""orch check: check the ticket files"""

from orch.ops._dsl import INT, STR, arr, obj, operation

OP = operation(
    "check",
    "Admin",
    "Check that the ticket files match their events (for a commit hook).",
    who="read",
    pre=("workspace_exists",),
    text="ok check {problems}\nnext: {next}",
    data=obj({"problems": INT, "findings": arr(obj({"where": STR, "what": STR}))}),
)
