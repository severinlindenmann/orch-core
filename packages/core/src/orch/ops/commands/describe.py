"""orch describe: the full contract of one command, generated from the registry"""

from orch.ops._dsl import STR, L, err, operation

OP = operation(
    "describe",
    "Context",
    "The full contract of one command (or the list of commands), generated from the registry.",
    who="read",
    props={"cmd": L("command, for example task done or task.done; leave out for the list", **{"x-metavar": "CMD"})},
    positional=("cmd",),
    text="ok describe {target}\nnext: {next}",
    data={
        "type": "object",
        "properties": {"target": STR, "name": STR, "cli": STR, "summary": STR, "who": STR},
        "required": ["target"],
    },
    errors=(err("not_found", "orch describe lists the commands", ["orch", "describe"]),),
)


def _handle(ctx, args):
    """Implemented in orch.cli (it needs the registry and the argument rules)."""
    from orch.cli.meta import describe as run

    return run(ctx, args)


OP = OP.__class__(declaration=OP.declaration, group=OP.group, handler=_handle)
