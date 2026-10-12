"""orch wait: wait for a decision"""

import time
from dataclasses import dataclass, field
from typing import Any

from orch.ops._dsl import REF_PATTERN, B, I, S, err, operation
from orch.ops.base import Context, Result
from orch.ops.decisions import decision
from orch.ops.errors import OrchError
from orch.ops.runtime import Call
from orch.ops.views import fence

POLL = (0.1, 0.25, 0.5, 1.0)  # seconds between looks at the log, growing


HINT = {
    "answered": "orch task next",
    "approved": "orch task next",
    "changes_requested": "orch show --log",
    "invalidated": "orch show",
    "timeout": "orch wait",
}


class Parked(Exception):  # noqa: N818 - a signal, not an error
    """Host mode: no decision yet; the host parks the request on its bus and calls again (``Step``)."""


@dataclass
class Step:
    """Host mode (H6a): the state of one ``wait`` across its looks. ``after`` is fixed by the first look; ``log`` is the
    ticket uid the host watches; ``expired`` makes the next look give the timeout answer."""

    after: int | None = None
    log: str | None = None
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
        step.after, step.log = after, view.uid
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
    errors=(err("wait.timeout"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
