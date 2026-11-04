"""orch wait: wait for a decision"""

import time
from dataclasses import dataclass, field
from typing import Any

from orch.model.claims import in_family
from orch.ops._dsl import REF_PATTERN, B, I, S, err, operation
from orch.ops.base import Context, Result
from orch.ops.decisions import decision
from orch.ops.errors import OrchError
from orch.ops.runtime import Call
from orch.ops.views import fence, gap_commands, next_hint, open_gates, open_questions

POLL = (0.1, 0.25, 0.5, 1.0)  # seconds between looks at the log, growing


HINT = {
    "answered": "orch task next",
    "approved": "orch task next",
    "changes_requested": "orch show --log",
    "invalidated": "orch show",
    "timeout": "orch wait",
}


def unfinished(c: Call, view: Any) -> OrchError | None:
    """Run-10 J04: the refusal for a wait nobody can answer. The caller's session holds the claim, nothing else is
    open for a person (no open blocking question, not testing/done/closed) and the first open gate still lacks items:
    the person cannot approve anything (the plan is approved after the requirements), so the wait would never end.
    The message names each missing item and the command that fills it (views.gap_commands); an artifact has no
    one-line command: ``orch show``."""
    if view.status in ("testing", "done", "closed") or any(q.blocking for q in open_questions(view)):
        return None
    if not holder(c, view):
        return None
    gates = open_gates(view)
    if not gates:
        return built(c, view)
    if not view.gates[gates[0]].missing:
        return None  # the first open gate is complete: the person can approve it (the plan only after requirements)
    parts: list[str] = []
    first: str | None = None
    for g in gates:
        cmds = dict(gap_commands(view, g))
        for m in view.gates[g].missing:
            if m in cmds:
                parts.append(f"{m} - fill it ({cmds[m]})")
                first = first or cmds[m]
            else:
                parts.append(f"{m} - add it (orch show)")
    if not parts:
        return None
    hint = f"{first or 'orch show'}, then orch wait"
    msg = f"missing: {'; '.join(parts)}, then wait: a person can approve only a complete {view.key}"
    return OrchError("wait.incomplete", msg, hint=hint)


def holder(c: Call, view: Any) -> bool:
    """Does the caller's session (or its subagent) hold the ticket's claim?"""
    session = getattr(c.ctx, "session", None)
    return view.claim is not None and bool(session) and in_family(session, view.claim.session)


def built(c: Call, view: Any) -> OrchError | None:
    """Run-10 review: the plan is approved and no gate is open: nobody decides anything before the build, so a wait
    would park forever. Refuse with the next step (views.next_hint), unless that step is itself a wait."""
    plan = view.gates["plan"]
    if not (plan.applies and plan.approved):
        return None
    hint = next_hint(view, getattr(c.ctx, "session", None))
    if hint == "orch wait":
        return None
    return OrchError(
        "wait.build", f"the plan of {view.key} is approved: build now ({hint}); nothing waits for a person", hint=hint
    )


class Parked(Exception):  # noqa: N818 - a signal, not an error
    """Host mode: no decision yet; the host parks the request on its bus and calls again (``Step``)."""


@dataclass
class Step:
    """Host mode (H6a): the state of one ``wait`` across its looks. ``after`` is fixed by the first look; ``log`` is the
    ticket uid the host watches; ``expired`` makes the next look give the timeout answer."""

    after: int | None = None
    log: str | None = None
    key: str | None = None  # the ticket key (the host's "agent waits" record matches a run by it)
    mine: bool = False  # the caller's session holds the claim: only then the host records "agent waits"
    expired: bool = False
    notes: list[str] = field(default_factory=list)  # the first look's observe notes, for the answer


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Wait for the first decision after the session's decision cursor, which only ``wait`` (and ``inbox``) moves: a
    decision ``show`` displayed is still handed over here. A session that never touched the ticket waits for what is
    decided from now on. The timeout is the caller's, 540 s by default; it ends in ``timeout`` (exit 0) so the agent
    loops, or in ``wait.timeout`` (exit 7) with ``--strict-timeout``."""
    c = Call.of(ctx, "wait")
    view = c.resolve(args.get("ref"), live_only=False)  # a done ticket ended the claim, not the wait
    step: Step | None = getattr(ctx, "wait_step", None)
    first_look = step is None or step.after is None  # hosted: only the first look may refuse
    if step is None or step.after is None:  # hosted: the first look only (git reader work), as P1 does once per call
        problems: list[str] = []
        c.ctx.caps.git.observe(c.store, view.uid, problems)
        notes_ = [f"observe: {x}" for x in problems]
        if step is not None:
            step.notes = notes_
    else:
        notes_ = list(step.notes)
    note = c.notes.get(view.uid)
    if step is not None and step.after is not None:
        after = step.after  # a later look of one hosted wait
    else:
        start = c.store.head_seq(view.uid)
        if note["decided"] is None:  # the session never touched this ticket: wait for what is decided from now on
            c.notes.update(view.uid, now=c.now, first_decided=start)
        after = start if note["decided"] is None else note["decided"]
    if step is not None:
        step.after, step.log, step.key = after, view.uid, view.key
        if first_look:
            step.mine = holder(c, view)
    deadline = time.monotonic() + args.get("timeout", 540)
    pause = iter(POLL)
    wait = POLL[0]
    while True:
        for e in c.store.events(view.uid, after=after):
            got = decision(e, view.key)
            if got is None:
                continue
            fields, code = got
            c.notes.update(view.uid, now=c.now, cursor=e["seq"], decided=e["seq"], keep=False)
            hints = [HINT.get(fields["kind"]) or ("orch show" if fields.get("outcome") == "pass" else "orch task next")]
            data = {**fields, "cursor": e["seq"], "next": hints[0]}
            lines = notes_ + (fence(fields["text"], "decision text") if fields.get("text") else [])
            return Result(data=data, key=view.key, seq=e["seq"], cursor=e["seq"], hints=hints, lines=lines, exit=code)
        if first_look:  # no decision to hand over: refuse a wait on a gate that lacks items (nobody can approve it)
            first_look = False
            refused = unfinished(c, view)
            if refused is not None:
                raise refused
        if step is not None:  # hosted: never sleep on the worker thread; the host wakes us on the next entry
            if not step.expired:
                raise Parked
            break
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(wait, max(0.0, deadline - now)))
        wait = next(pause, POLL[-1])
    if args.get("strict_timeout"):
        raise OrchError("wait.timeout", f"no decision on {view.key} within {args.get('timeout', 540)} s")
    cursor = c.cursor(view.uid)
    data = {"kind": "timeout", "key": view.key, "cursor": cursor, "next": HINT["timeout"]}
    return Result(
        data=data, key=view.key, seq=c.store.head_seq(view.uid), cursor=cursor, hints=[HINT["timeout"]], lines=notes_
    )


OP = operation(
    "wait",
    "Lifecycle",
    "Wait for an answer, an approval, a change request, a verdict or an invalidation (default 540 s).",
    who="read",
    props={
        "ref": S("ticket REF (flag); default: your claim", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
        "timeout": I("seconds to wait", minimum=1, maximum=3600, default=540, **{"x-metavar": "SECONDS"}),
        "strict_timeout": B("exit 7 when the timeout passes"),
    },
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} {kind} cursor={cursor}\nnext: {next}",
    output_ref="https://schemas.orch.dev/v2/wait-result",
    errors=(err("wait.timeout"), err("wait.incomplete"), err("wait.build"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
