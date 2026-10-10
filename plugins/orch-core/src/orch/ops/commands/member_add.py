"""orch member add: add a member"""

from orch.ops._dsl import STR, E, S, err, obj, operation

OP = operation(
    "member.add",
    "Human only",
    "Add a member with a role; their first device certificate comes with the invitation.",
    who="human",
    props={
        "name": S("display name", **{"x-metavar": "NAME"}),
        "role": E("role", "owner", "maintainer", "member", "viewer", default="member"),
        "pk": S("their public key, b64u", **{"x-metavar": "KEY"}),
        "cert": S("their device certificate: PATH or - for stdin", **{"x-metavar": "PATH"}),
    },
    required=("name", "pk", "cert"),
    pre=("workspace_exists", "role_allows", "user_presence", "text_clean"),
    emits=("member.added",),
    text="ok member.added {person} {role} seq={seq}\nnext: {next}",
    data=obj({"person": STR, "role": STR}),
    errors=(err("role.denied"), err("parse.json"), err("parse.text")),
)
