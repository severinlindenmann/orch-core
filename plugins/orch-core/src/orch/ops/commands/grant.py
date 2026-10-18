"""orch grant: issue a standing grant"""

import re
from typing import Any

from orch.identity import new_grant, new_ulid
from orch.model.types import GRANT_MAX_HOURS
from orch.ops import human
from orch.ops._dsl import STR, B, E, I, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human, iso

_VERB = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?")


def _issued(grant_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """The result data of an issued grant: ``until`` as signed (UTC, for machines) and ``until_local``, the same moment
    in this computer's time with its zone, which the text line shows."""
    from orch.host.signcontent import local_time

    until = event["expires_at"]  # the key order is the schema's: host mode answers in that order
    return {
        "grant": grant_id,
        "until": until,
        "until_local": local_time(until, event.get("hours")) or until,
        "scope": event["scope"],
    }


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
    if args.get("days") is not None:  # B8: --days N is 24 N hours
        if args.get("hours") is not None:
            raise OrchError("invalid.input", "--days and --hours are alternatives; give one")
        hours = args["days"] * 24
    else:
        hours = args.get("hours", 8)
    if not 1 <= hours <= GRANT_MAX_HOURS:
        raise OrchError("invalid.input", f"a grant lasts 1 to {GRANT_MAX_HOURS} hours (7 days)")
    h = Human(ctx, "grant")
    if args.get("runner"):  # H11c (D-P2-27): a runner grant, handed to the orch runner instead of being printed
        if ctx.prepare is None:
            raise OrchError("invalid.input", "--runner hands the grant to the orch runner, which needs the orch host")
        cap = min(h.store.state.workspace.settings["grant_hours"], GRANT_MAX_HOURS)
        if hours > cap or args.get("verbs", "agent") != "agent" or args.get("label", "runner") != "runner":
            raise OrchError(
                "invalid.input",
                f"a runner grant lasts at most {cap} hours (the workspace setting), verbs agent, label runner",
            )
        args = {**args, "label": "runner"}
    issued = ctx.now()
    if ctx.prepare is not None:  # human.prepare: the signing client draws the secret, never the preparer (F1, D-P2-16)
        grant_id, value, secret_hash = "gr_" + new_ulid(int(issued * 1000)), None, None
    else:
        token, value, secret_hash = new_grant(int(issued * 1000))
        grant_id = token.grant_id
    event: dict[str, Any] = {
        "type": "grant.issued",
        "grant": grant_id,
        "scope": args.get("scope", "workable"),
        "verbs": _verbs(args.get("verbs", "agent")),
        "issued_at": iso(issued),
        "hours": hours,
        "expires_at": iso(int(issued) + 3600 * hours),
        "secret_hash": secret_hash,
    }
    if args.get("label"):
        event["label"] = h.call.text(args["label"], one_line=True, what="label")
    old = args.get("extend")
    if old is not None:  # B8: Extend = the new runner grant and the revoke of the old one, one prepared request
        person, _ = h.who()
        was = h.store.state.workspace.grants.get(old)
        if not args.get("runner") or was is None or was.label != "runner" or was.person != person or was.revoked:
            raise OrchError("invalid.input", "--extend replaces your own live runner grant (with --runner)")
    done = h.run(event, "workspace", f"issue grant {grant_id} for {hours} h")
    if old is not None:  # judged with the first leg by human.submit (judge_events): both land or neither does
        h.run({"type": "grant.revoked", "grant": old, "reason": "extended"}, "workspace", f"revoke grant {old}")
    lines: list[str] = []
    if ctx.prepare is not None and ctx.prepare.dry_run:  # as below: a dry run made no grant
        return h.workspace_result(None, {"scope": event["scope"]}, None, ["no grant was made"])
    if ctx.prepare is not None and args.get("runner"):  # H11c: the signing client hands it to the runner
        return h.workspace_result(
            None,
            _issued(grant_id, event),
            "start agents from the dashboard: the orch runner holds the grant",
            [],
        )
    if ctx.prepare is not None:  # the result human.submit answers with once the signed grant landed
        # the signing client shows the secret right after the commit and renders this result only once it has (H7:
        # the same line and hint as direct mode)
        return h.workspace_result(
            None,
            _issued(grant_id, event),
            "set ORCH_GRANT=<the value shown> for the harness",
            ["the secret was shown on your terminal; clear its scrollback when the harness has it"],
        )
    if done is not None:
        try:
            human.show_secret(  # looked up at call time: a test replaces it, nothing else does
                f"\nORCH_GRANT={value}\n(shown once; orch keeps only its hash)\n"
                "Clear your terminal scrollback when the harness has it: whatever can read this terminal can read it.\n"
            )
        except Exception as e:  # the grant exists but nobody can use it: say how to end it
            raise OrchError(
                "custody.no_prompt",
                f"{grant_id} was issued but its secret could not be shown ({e}); revoke it: orch grant revoke "
                f"{grant_id}",
            ) from None
        lines.append("the secret was shown on your terminal; clear its scrollback when the harness has it")
        data = _issued(grant_id, event)
        return h.workspace_result(done, data, "set ORCH_GRANT=<the value shown> for the harness", lines)
    # a dry run made no grant: no id, no end time, no hint (the id above was never used)
    return h.workspace_result(None, {"scope": event["scope"]}, None, ["no grant was made"])


OP = operation(
    "grant",
    "Human only",
    "Issue a standing grant for agents; the secret is printed once, on your terminal.",
    who="human",
    props={
        "hours": I("length in whole hours (1 to 168)", minimum=1, maximum=GRANT_MAX_HOURS, **{"x-metavar": "N"}),
        "days": I("length in whole days (1 to 7); instead of --hours", minimum=1, maximum=7, **{"x-metavar": "N"}),
        "scope": E("what it covers", "all", "workable", default="workable"),
        "verbs": S("agent, or operation names comma separated", default="agent", **{"x-metavar": "VERBS"}),
        "label": S("short label", **{"x-metavar": "TEXT"}),
        "extend": S(
            "revoke this runner grant of yours together with the new one (with --runner)",
            pattern=r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$",
            **{"x-metavar": "GRANT"},
        ),
        "runner": B("hand the secret to the runner here instead of showing it (at most the grant_hours setting)"),
    },
    pre=("workspace_exists", "role_allows", "user_presence", "text_clean"),
    emits=("grant.issued", "grant.revoked"),
    text="ok grant.issued[ {grant}][ until {until_local}]\nnext: {next}",
    data=obj(
        {"grant": STR, "until": STR, "until_local": STR, "scope": STR}, optional=("grant", "until", "until_local")
    ),
    errors=(err("role.denied"), err("parse.text")),
    handler=handle,
)
