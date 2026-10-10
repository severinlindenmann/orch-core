"""orch artifact replace: replace a stored file"""

from orch.ops._dsl import INT, REF_PATTERN, STR, S, err, obj, operation

OP = operation(
    "artifact.replace",
    "Edit",
    "Replace a stored file; the name stays, the digest changes.",
    who="agent",
    props={
        "name": S("artifact name", **{"x-metavar": "NAME"}),
        "path": S("the new file", **{"x-metavar": "PATH"}),
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("name", "path"),
    positional=("name", "path"),
    pre=("ticket_exists", "ticket_open_for_work", "grant_valid"),
    emits=("artifact.replaced",),
    text="ok {key} artifact.replaced {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR, "sha256": STR, "bytes": INT}),
    errors=(err("transition.refused"), err("not_found"), err("ambiguous_ref")),
)
