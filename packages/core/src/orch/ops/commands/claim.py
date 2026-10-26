"""orch claim: take the ticket's claim"""

from typing import Any

from orch.ops import views
from orch.ops._dsl import BOOL, REF, STR, B, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import _TASK_ONLY, Call
from orch.schema import SECTIONS_BY_TYPE


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "claim")
    explicit = None if args.get("next") else args.get("ref")
    if args.get("reason") and not args.get("takeover"):
        raise OrchError("invalid.input", "--reason goes with --takeover")
    with c.locked():
        if explicit is not None:
            if "/" in explicit or _TASK_ONLY.fullmatch(explicit):
                raise OrchError("invalid.input", "claim takes a ticket REF, not a task")
            view = c.resolve(explicit)
        else:
            view = views.next_ticket(c, mine_first=False)
            if view is None:
                raise OrchError("not_found", "no ticket is free to claim: orch list --status open")
        from orch.model.claims import in_family

        held = view.claim
        if held is not None and held.live and c.ctx.session and in_family(c.ctx.session, held.session):
            c.notes.add_claim(view.uid)
            return c.result(
                view, {"status": view.status}, hints=[views.next_hint(view, ctx.session)], lines=["already your claim"]
            )
        mine = [v for v in c.mine() if v.uid != view.uid]
        if mine and not args.get("also"):
            raise OrchError(
                "claim.held",
                f"you already hold {', '.join(v.key for v in mine)}; with --also you hold several and every command "
                "then needs a REF",
                hint="orch claim REF --also, or orch release first",
            )
        event: dict[str, Any] = {"type": "claim.taken"}
        if args.get("takeover"):
            if held is None:
                raise OrchError("invalid.input", "nothing to take over: the ticket has no claim")
            if not args.get("reason"):
                raise OrchError("invalid.input", "--takeover needs --reason")
            event["takeover"] = {
                "from_session": held.session,
                "reason": c.text(args["reason"], one_line=True, what="reason"),
            }
        elif held is not None and view.status == "in_progress":
            why = "held by another session" if held.live else f"the claim lapsed ({held.lapsed}); it is still held"
            raise OrchError(
                "claim.held",
                f"{view.key}: {why}. To work on it from this shell session anyway, run "
                f"orch claim {view.key} --takeover --reason TEXT",
            )
        p = c.projection(view)
        p.add(event)
        done = p.commit()
        if done:
            c.notes.add_claim(view.uid)
            c.shown(p.last_view, fields=views.ALL_FIELDS, sections=tuple(SECTIONS_BY_TYPE[view.type]))
        data: dict[str, Any] = {"status": p.last_view.status}
        if "takeover" in event:
            data["takeover"] = True
        seq = done[-1].event["seq"] if done else p.last_seq
        lines: list[str] = []
        if done and not ctx.dry_run:
            from orch.ops.workdir import workdir_of

            wd = workdir_of(c.store, p.last_view, create=True)  # no codebase: the work folder, made if missing
            if wd is not None:
                lines.append(f"workdir: {wd} (write results here; orch artifact add FILE --task Tn --ac ACn)")
        return c.result(p.last_view, data, seq=seq, hints=[views.next_hint(p.last_view, ctx.session)], lines=lines)


OP = operation(
    "claim",
    "Lifecycle",
    "Claim a ticket, the next one, or take over another session's claim.",
    who="agent",
    props={
        "ref": REF("ticket REF; default: the next ticket"),
        "next": B("claim the next ticket"),
        "takeover": B("take the claim from another session (needs --reason)"),
        "also": B("hold this claim besides the ones you already hold"),
        "reason": S("why you take over", **{"x-metavar": "TEXT"}),
    },
    positional=("ref",),
    pre=("ticket_exists", "ticket_visible", "ticket_open_for_work", "claim_free_or_takeover", "grant_valid"),
    emits=("claim.taken",),
    text="ok {key} claim.taken {status} seq={seq}\nnext: {next}",
    data=obj({"status": STR, "takeover": BOOL}, optional=("takeover",)),
    errors=(err("claim.held"), err("not_found"), err("transition.refused")),
    handler=handle,
)
