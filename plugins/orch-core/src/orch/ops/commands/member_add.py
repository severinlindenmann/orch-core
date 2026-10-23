"""orch member add: add a member"""

from typing import Any

from orch import canon, crypto
from orch.identity import person_ref
from orch.ops._dsl import STR, E, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human
from orch.ops.readers import ReadersRefused, check_readers, required_reader, verify_admission

#: I2 (#463 review): a declaration file is at most this many bytes, checked **before** it is parsed (2 KiB, the cap of
#: the signed object, plus a margin for spacing and a trailing newline)
DECL_FILE_MAX = 4096


def _reader(h: Human, args: dict[str, Any], cert: dict[str, Any], person: str) -> dict[str, Any] | None:
    """The invitee's reader declaration (``--reader-decl PATH``, §0.8.6 item 9.4, ``via:"enrol"``), verified against
    the request (the certificate's keys, the person, this workspace, the ``log`` KAT, the 30-day window), or ``None``
    when none was given. A bad one is ``reader.decl_invalid``."""
    from orch.identity.reader import DeclInvalid

    source = args.get("reader_decl")
    if source is None:
        return None
    raw = h.call.read_file_capped(source, DECL_FILE_MAX)
    try:
        decl = canon.loads_strict(raw)
    except (canon.JcsError, ValueError):
        raise OrchError("reader.decl_invalid", "shape: --reader-decl is not strict JSON") from None
    try:
        got = verify_admission(
            decl,
            workspace_id=h.workspace_id,
            cert=cert,
            person=person,
            via="enrol",
            surfaces=("log",),
            now_ms=int(h.ctx.now() * 1000),
        )
    except DeclInvalid as exc:
        raise OrchError("reader.decl_invalid", exc.detail) from None
    return {
        "device": "d_" + got.device_id,
        "person": person,
        "decl": decl,
        "source": "member.add",
        "reader": got.reader,
        "level": got.level,
        "digest": got.digest,
    }


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
        cert = canon.loads_strict(h.call.read_file_text(args["cert"], key_file="device_cert").encode("utf-8"))
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
    entry = _reader(h, args, cert, person)
    # READER_OPS (§0.8.6 item 9.4): the new reader's level against required_reader, early here; the append checks it
    # again under the store lock (both modes). Direct mode asks the durable mark (#441) and the history; the host adds
    # its flag and mark itself (refuse_reader_op)
    state_dir = None if h.call.ws.hosted else h.call.ws.state_dir
    required = required_reader(h.store, state_dir, h.workspace_id)
    device = "d_" + str((cert.get("o") or {}).get("device_id")) if isinstance(cert, dict) else "d_?"
    try:
        check_readers(
            h.store.state.workspace,
            {device: entry["level"]} if entry else {},
            [event],
            required=required,
            relay_level=None,
        )
    except ReadersRefused as exc:
        raise exc.orch_error() from None
    if entry is not None:
        h.admit_reader(entry)
    level = entry["level"] if entry else 0
    reader_line = f"reader level {level}" + (" (understands convenience signing)" if level >= 1 else " (old)")
    done = h.run(event, "workspace", f"add member {person} as {event['role']}; {reader_line}")
    hint = f"check with {event['name']}, out of band, that their person id is {person}"
    return h.workspace_result(done, {"person": person, "role": event["role"]}, hint)


OP = operation(
    "member.add",
    "Human only",
    "Add a member with a role; their first device certificate comes with the invitation.",
    who="human",
    props={
        "name": S("display name", **{"x-metavar": "NAME"}),
        "role": E("role", "owner", "maintainer", "member", "viewer", default="member"),
        "pk": S("their public key, b64u", **{"x-metavar": "KEY"}),
        "cert": S("their device certificate: PATH or - for stdin", **{"x-metavar": "PATH", "x-file": True}),
        "reader_decl": S("their reader declaration: PATH", **{"x-metavar": "PATH", "x-file": True}),
    },
    required=("name", "pk", "cert"),
    pre=("workspace_exists", "role_allows", "user_presence", "text_clean"),
    emits=("member.added",),
    text="ok member.added {person} {role} seq={seq}\nnext: {next}",
    data=obj({"person": STR, "role": STR}),
    errors=(
        err("role.denied"),
        err("parse.json"),
        err("parse.text"),
        err("sign.convenience_readers"),
        err("reader.decl_invalid"),
    ),
    handler=handle,
)
