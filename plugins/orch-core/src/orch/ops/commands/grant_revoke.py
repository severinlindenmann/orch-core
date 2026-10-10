"""orch grant revoke: revoke a grant"""

from orch.ops._dsl import STR, S, err, obj, operation

OP = operation(
    "grant.revoke",
    "Human only",
    "Revoke a grant; every session using it ends.",
    who="human",
    props={
        "grant": S("grant id", pattern=r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$", **{"x-metavar": "GRANT"}),
        "reason": S("why", **{"x-metavar": "TEXT"}),
    },
    required=("grant",),
    positional=("grant",),
    pre=("workspace_exists", "role_allows", "user_presence", "text_clean"),
    emits=("grant.revoked",),
    text="ok grant.revoked {grant} seq={seq}\nnext: {next}",
    data=obj({"grant": STR}),
    errors=(err("role.denied"), err("parse.text"), err("not_found")),
)
