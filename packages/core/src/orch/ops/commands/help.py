"""orch help: how to do one kind of work, generated from the registry"""

from orch.ops._dsl import STR, S, arr, err, obj, operation

OP = operation(
    "help",
    "Context",
    "How to do one kind of work (a workflow), generated from the registry; no argument lists them.",
    who="read",
    props={"workflow": S("workflow name", **{"x-metavar": "WORKFLOW"})},
    positional=("workflow",),
    text="ok help {topic}\nnext: {next}",
    data=obj(
        {"topic": STR, "workflows": arr(STR), "steps": arr(obj({"op": STR, "call": STR}))},
        optional=("workflows", "steps"),
    ),
    errors=(err("not_found", "orch help lists the workflows", ["orch", "help"]),),
)


def _handle(ctx, args):
    """Implemented in orch.cli (it needs the registry and the argument rules)."""
    from orch.cli.meta import help as run

    return run(ctx, args)


OP = OP.__class__(declaration=OP.declaration, group=OP.group, handler=_handle)
