"""orch instructions hook: the text a harness hook injects at session start and before compaction"""

from typing import Any

from orch.ops import decisions, views
from orch.ops._dsl import STR, B, E, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Workspace, flat, short


def _silent(event: str) -> Result:
    return Result(data={"event": event, "head": "", "silent": True})


def _split(event: str, lines: list[str]) -> Result:
    """``lines`` as the CLI prints them: the first is ``ok <head>``, the last ``next: <hint>``."""
    head, middle, hint = lines[0][len("ok ") :], lines[1:-1], lines[-1][len("next: ") :]
    return Result(data={"event": event, "head": head}, hints=[hint], lines=middle)


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    from orch.instructions import pre_compact_lines, session_start_lines, stale_findings
    from orch.ops.commands import status

    event = args["event"]
    ws = ctx.workspace or Workspace(ctx.env, ctx.now)
    if ws.root is None:  # a plugin hook runs in every project: outside a workspace it says nothing
        return _silent(event)
    try:
        st = status.handle(ctx, {})
        c = Call.of(ctx, "status")
        mine = c.mine() if ctx.session else []
        unread: list[str] = []
        for v in mine:
            unread += [f"{v.key} {decisions.line(e, 40)}" for e in decisions.undelivered(c, v)]
        claim_line = st.lines[0] if st.lines and st.lines[0] != "no claim" else None
        if event == "pre-compact":
            open_q = sum(len(views.open_questions(v)) for v in mine)
            return _split(event, pre_compact_lines(claim=st.data.get("claim"), open_questions=open_q))
        lines = session_start_lines(
            person=flat(st.data["person"]),
            grant=st.data.get("grant"),
            claim=st.data.get("claim"),
            claim_line=short(claim_line, 110) if claim_line else None,
            unread=[short(u, 60) for u in unread],
            stale=bool(stale_findings(ws.root)),
            next_hint=st.hints[0] if st.hints else "orch status",
        )
        return _split(event, lines)
    except OrchError as e:
        return _split(event, [f"ok {event} unavailable ({e.code})", "next: orch status"])


OP = operation(
    "instructions.hook",
    "Admin",
    "The text a harness hook injects at session start or before compaction: at most six lines.",
    who="read",
    props={"event": E("which hook", "session-start", "pre-compact", **{"x-metavar": "EVENT"})},
    required=("event",),
    positional=("event",),
    pre=(),
    text="ok {head}\nnext: {next}",
    data=obj({"event": STR, "head": STR, "silent": B("print nothing")}, optional=("silent",)),
    handler=handle,
)
