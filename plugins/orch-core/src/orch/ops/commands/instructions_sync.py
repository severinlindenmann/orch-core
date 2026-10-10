"""orch instructions sync: refresh AGENTS.orch.md"""

from orch.ops._dsl import STR, arr, obj, operation

OP = operation(
    "instructions.sync",
    "Admin",
    "Rewrite AGENTS.orch.md and the skills to the installed version.",
    who="agent",
    pre=("workspace_exists", "grant_valid"),
    text="ok instructions.sync {version}\nnext: {next}",
    data=obj({"version": STR, "files": arr(STR)}),
)
