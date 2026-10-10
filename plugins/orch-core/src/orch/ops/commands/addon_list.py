"""orch addon list: list the addons"""

from orch.ops._dsl import BOOL, INT, STR, arr, obj, operation

OP = operation(
    "addon.list",
    "Admin",
    "List the installed addons and whether each is enabled.",
    who="read",
    pre=("workspace_exists",),
    text="ok addon.list {count}\nnext: {next}",
    data=obj({"count": INT, "addons": arr(obj({"name": STR, "version": STR, "enabled": BOOL}))}),
)
