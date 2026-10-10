"""orch grant: issue a standing grant"""

from orch.ops._dsl import STR, E, I, S, err, obj, operation

OP = operation(
    "grant",
    "Human only",
    "Issue a standing grant for agents; the secret is printed once, on your terminal.",
    who="human",
    props={
        "hours": I("length in whole hours (1 to 24)", minimum=1, maximum=24, default=8, **{"x-metavar": "N"}),
        "scope": E("what it covers", "all", "workable", default="workable"),
        "verbs": S("agent, or operation names comma separated", default="agent", **{"x-metavar": "VERBS"}),
        "label": S("short label", **{"x-metavar": "TEXT"}),
    },
    pre=("workspace_exists", "role_allows", "user_presence", "text_clean"),
    emits=("grant.issued",),
    text="ok grant.issued {grant} until={until}\nnext: {next}",
    data=obj({"grant": STR, "until": STR, "scope": STR}),
    errors=(err("role.denied"), err("parse.text")),
)
