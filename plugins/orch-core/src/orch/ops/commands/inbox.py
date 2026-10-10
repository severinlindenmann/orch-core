"""orch inbox: tickets handed over from peer workspaces"""

from orch.ops._dsl import INT, KEY, STR, arr, obj, operation

OP = operation(
    "inbox",
    "Read",
    "Tickets handed over from peer workspaces.",
    who="read",
    pre=("workspace_exists",),
    text="ok inbox {count}\nnext: {next}",
    data=obj({"count": INT, "items": arr(obj({"key": KEY, "from": STR, "title": STR}))}),
)
