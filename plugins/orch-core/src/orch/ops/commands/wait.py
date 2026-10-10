"""orch wait: wait for a decision"""

import time
from typing import Any

from orch.cli.render import fence
from orch.ops._dsl import REF_PATTERN, B, I, S, err, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call

POLL = (0.1, 0.25, 0.5, 1.0)  # seconds between looks at the log, growing


def _by(e: dict[str, Any]) -> str:
    return e["actor"]["id"]


def decision(e: dict[str, Any], key: str) -> tuple[dict[str, Any], int] | None:
    """What a ticket event means for a waiting agent (F1 10.4 item 7), as ``(fields, exit code)``; ``None`` when it
    is not a decision. The fields are exactly the ones the item lists for the kind and nothing else."""
    t = e["type"]
    base = {"key": key, "seq": e["seq"]}
    if t == "question.answered":
        out = {"kind": "answered", **base, "question": e["question"], "by": _by(e)}
        for k in ("option", "text"):
            if e.get(k) is not None:
                out[k] = e[k]
        return out, 0
    if t == "gate.approved":
        return {"kind": "approved", **base, "gate": e["gate"], "by": _by(e)}, 0
    if t == "gate.changes_requested":
        return {"kind": "changes_requested", **base, "gate": e["gate"], "text": e["text"], "by": _by(e)}, 3
    if t == "verdict.given":
        out = {"kind": "verdict", **base, "outcome": e["outcome"], "by": _by(e)}
        if e["outcome"] == "fail":
            out["text"] = e["text"]
        return out, 3 if e["outcome"] == "fail" else 0
    if t == "gate.invalidated":
        return {"kind": "invalidated", **base, "gate": e["gate"]}, 3
    return None


HINT = {
    "answered": "orch task next",
    "approved": "orch task next",
    "changes_requested": "orch show --log",
    "invalidated": "orch show",
    "timeout": "orch wait",
}


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Wait for the first decision after the session's last write on the ticket (or after the head, when the session
    never wrote or read it). The timeout is the caller's, 540 s by default; it ends in ``timeout`` (exit 0) so the agent
    loops, or in ``wait.timeout`` (exit 7) with ``--strict-timeout``."""
    c = Call.of(ctx, "wait")
    view = c.resolve(args.get("ref"))
    note = c.notes.get(view.uid)
    start = c.store.head_seq(view.uid)
    after = max(note["cursor"], note["since"] or 0) if (note["cursor"] or note["since"] is not None) else start
    deadline = time.monotonic() + args.get("timeout", 540)
    pause = iter(POLL)
    wait = POLL[0]
    while True:
        for e in c.store.events(view.key, after=after):
            got = decision(e, view.key)
            if got is None:
                continue
            fields, code = got
            c.notes.update(view.uid, now=c.now, cursor=e["seq"])
            hints = [HINT.get(fields["kind"]) or ("orch show" if fields.get("outcome") == "pass" else "orch task next")]
            data = {**fields, "cursor": e["seq"], "next": hints[0]}
            lines = fence(fields["text"], "decision text") if fields.get("text") else []
            return Result(data=data, key=view.key, seq=e["seq"], cursor=e["seq"], hints=hints, lines=lines, exit=code)
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(wait, max(0.0, deadline - now)))
        wait = next(pause, POLL[-1])
    if args.get("strict_timeout"):
        raise OrchError("wait.timeout", f"no decision on {view.key} within {args.get('timeout', 540)} s")
    cursor = c.cursor(view.uid)
    data = {"kind": "timeout", "key": view.key, "cursor": cursor, "next": HINT["timeout"]}
    return Result(data=data, key=view.key, seq=c.store.head_seq(view.uid), cursor=cursor, hints=[HINT["timeout"]])


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
    text="ok {key} wait {kind} cursor={cursor}\nnext: {next}",
    output_ref="https://schemas.orch.dev/v2/wait-result",
    errors=(err("wait.timeout"), err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
