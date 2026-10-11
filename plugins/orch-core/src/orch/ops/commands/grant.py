"""orch grant: issue a standing grant"""

import re
from typing import Any

from orch.identity import new_grant
from orch.ops import human
from orch.ops._dsl import STR, E, I, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human, iso

_VERB = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?")


def _verbs(raw: str) -> str | list[str]:
    if raw == "agent":
        return "agent"
    names = raw.split(",")
    if any(not _VERB.fullmatch(n) for n in names) or len(set(names)) != len(names):
        raise OrchError("invalid.input", "--verbs is agent, or operation names (task.done, log) comma separated")
    import orch.ops as ops

    known = {n for n in ops.names() if ops.get(n).who in ("agent", "unattended")}
    if bad := [
        n for n in names if n not in known
    ]:  # a human operation is never in a grant (F1 10.1); a name no agent runs grants nothing
        raise OrchError("invalid.input", f"--verbs: {', '.join(bad)} is not an operation an agent may run")
    return names


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The grant secret is made here, hashed into the event, and shown once on the person's own terminal after the
    append. It is in no result, no record, no log and no file; only ``secret_hash`` is stored (F1 10.1, N4)."""
    h = Human(ctx, "grant")
    hours = args.get("hours", 8)
    issued = ctx.now()
    token, value, secret_hash = new_grant(int(issued * 1000))
    event: dict[str, Any] = {
        "type": "grant.issued",
        "grant": token.grant_id,
        "scope": args.get("scope", "workable"),
        "verbs": _verbs(args.get("verbs", "agent")),
        "issued_at": iso(issued),
        "hours": hours,
        "expires_at": iso(int(issued) + 3600 * hours),
        "secret_hash": secret_hash,
    }
    if args.get("label"):
        event["label"] = h.call.text(args["label"], one_line=True, what="label")
    done = h.run(event, "workspace", f"issue grant {token.grant_id} for {hours} h")
    lines: list[str] = []
    if done is not None:
        try:
            human.show_secret(  # looked up at call time: a test replaces it, nothing else does
                f"\nORCH_GRANT={value}\n(shown once; orch keeps only its hash)\n"
                "Clear your terminal scrollback when the harness has it: whatever can read this terminal can read it.\n"
            )
        except Exception as e:  # the grant exists but nobody can use it: say how to end it
            raise OrchError(
                "custody.no_prompt",
                f"{token.grant_id} was issued but its secret could not be shown ({e}); revoke it: orch grant revoke "
                f"{token.grant_id}",
            ) from None
        lines.append("the secret was shown on your terminal; clear its scrollback when the harness has it")
        data = {"grant": token.grant_id, "until": event["expires_at"], "scope": event["scope"]}
        return h.workspace_result(done, data, "set ORCH_GRANT=<the value shown> for the harness", lines)
    # a dry run made no grant: no id, no end time, no hint (the id above was never used)
    return h.workspace_result(None, {"scope": event["scope"]}, None, ["no grant was made"])


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
    text="ok grant.issued[ {grant}][ until={until}]\nnext: {next}",
    data=obj({"grant": STR, "until": STR, "scope": STR}, optional=("grant", "until")),
    errors=(err("role.denied"), err("parse.text")),
    handler=handle,
)
