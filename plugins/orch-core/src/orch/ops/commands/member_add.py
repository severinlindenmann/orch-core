"""orch member add: add a member"""

from typing import Any

from orch import canon, crypto
from orch.identity import person_ref
from orch.ops._dsl import STR, E, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The invitee made their own person key and first device certificate; the owner signs only the roster change."""
    h = Human(ctx, "member.add")
    h.who()
    try:
        pk = crypto.unb64u(args["pk"], crypto.PUB_LEN)
        crypto.validate_public_key(pk)
    except (crypto.CryptoError, ValueError):
        raise OrchError("invalid.input", "--pk is a public key, base64url of 65 bytes") from None
    try:
        cert = canon.loads_strict(h.call.read_file_text(args["cert"]).encode("utf-8"))
    except (canon.JcsError, ValueError):
        raise OrchError("parse.json", "--cert is a device certificate, strict JSON") from None
    person = person_ref(pk)
    event: dict[str, Any] = {
        "type": "member.added",
        "person": person,
        "name": h.call.text(args["name"], one_line=True, what="name"),
        "role": args.get("role", "member"),
        "pk_pub": args["pk"],
        "device_cert": cert,
    }
    done = h.run(event, "workspace", f"add member {person} as {event['role']}")
    return h.workspace_result(done, {"person": person, "role": event["role"]}, "orch member")


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
    handler=handle,
)
