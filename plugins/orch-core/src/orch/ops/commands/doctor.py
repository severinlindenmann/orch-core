"""orch doctor: check the workspace's health"""

from orch.ops._dsl import INT, STR, arr, obj, operation

OP = operation(
    "doctor",
    "Admin",
    "Check the logs, chains, projections and keys; reports, never changes.",
    who="read",
    pre=("workspace_exists",),
    text="ok doctor {problems}\nnext: {next}",
    data=obj({"problems": INT, "findings": arr(obj({"where": STR, "what": STR}))}),
)
