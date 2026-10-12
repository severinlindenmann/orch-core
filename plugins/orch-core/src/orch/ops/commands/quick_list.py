"""orch quick list: the quick tasks (tickets with the label quick) and how big each has grown"""

import time
from typing import Any

from orch.ops import quick, views
from orch.ops._dsl import INT, I, L, S, arr, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, flat

STATUSES = ["backlog", "open", "in_progress", "waiting", "testing", "done", "closed"]
_NULL_INT = {"type": ["integer", "null"]}


def _page_start(token: str | None) -> int:
    if token is None:
        return 0
    head, _, n = token.partition(":")
    if head != "o" or not n.isdigit():
        raise OrchError("invalid.input", "page is not a token of this list: restart from the first page")
    return int(n)


def _counts(ctx: Context, c: Call, view: Any, left: float) -> tuple[int | None, int | None]:
    """Commits and files of the ticket's branches through the reader; unknown (``None``) when it links no repo or
    branch or the reader cannot read it (never an error: the limits are display only)."""
    from orch.ops.diff_core import RepoReadError, diff_plan, diff_run

    if left <= 0 or not view.fields["links"]["repos"]:
        return None, None
    try:
        jobs = diff_plan(c.store, view, ctx.caps)
        return quick.counts(diff_run(ctx.caps, jobs, context=0, max_bytes=1024, budget=left))
    except (OrchError, RepoReadError):
        return None, None


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Every visible ticket labelled ``quick`` (closed ones only when asked), newest key first. The index gives
    candidates; each ticket printed is loaded, verified and checked against the label again."""
    c = Call.of(ctx, "quick.list")
    statuses = set(args.get("status") or STATUSES[:-1])  # like ticket.query: closed only when asked for
    limit = args.get("limit", 100)
    max_commits = args.get("max_commits", quick.DEFAULT_MAX_COMMITS)
    max_files = args.get("max_files", quick.DEFAULT_MAX_FILES)
    start = _page_start(args.get("page"))
    cands = sorted(
        c.store.index.query(
            "SELECT uid, key FROM tickets WHERE uid IN (SELECT uid FROM ticket_labels WHERE label = ?)", [quick.LABEL]
        ),
        key=lambda r: views.key_number(r[1]),
        reverse=True,
    )
    matches = [
        v
        for _uid, v in views.verified(c, (r[0] for r in cands), chunk=200)
        if quick.LABEL in v.fields["labels"] and v.status in statuses
    ]
    matches.sort(key=lambda v: views.key_number(v.key), reverse=True)
    deadline = time.monotonic() + quick.BUDGET_S  # all rows together, as the host's reader phase
    rows = []
    for v in matches[start : start + limit]:
        commits, files = _counts(ctx, c, v, deadline - time.monotonic())
        rows.append(
            {
                "key": v.key,
                "uid": v.uid,
                "title": flat(v.title),
                "type": v.type,
                "status": v.status,
                "priority": v.fields.get("priority"),
                "size": v.fields.get("size"),
                "labels": list(v.fields["labels"]),
                "owner": v.owner,
                "quick": {
                    "commits": commits,
                    "files": files,
                    "over": quick.over(commits, files, max_commits, max_files),
                },
            }
        )
    nxt = f"o:{start + limit}" if start + limit < len(matches) else None
    text, hints = quick.lines(rows, len(matches), nxt)
    return Result(data={"rows": rows, "next_page": nxt, "total": len(matches)}, hints=hints, lines=text)


OP = operation(
    "quick.list",
    "Read",
    "List quick tasks (tickets labelled quick) with their commit and file counts; the limits are display only.",
    who="read",
    props={
        "status": L(
            "only these statuses, comma separated (default: all but closed)",
            split=True,
            items={"enum": STATUSES},
            **{"x-metavar": "A,B"},
        ),
        "limit": I("at most this many rows", minimum=1, maximum=200, default=100, **{"x-metavar": "N"}),
        "page": S("the next_page of the previous answer", minLength=1, maxLength=512, **{"x-metavar": "TOKEN"}),
        "max_commits": I(
            "a row is over with more commits than this", minimum=1, maximum=100, default=1, **{"x-metavar": "N"}
        ),
        "max_files": I(
            "a row is over with more changed files than this", minimum=1, maximum=1000, default=3, **{"x-metavar": "N"}
        ),
    },
    pre=("workspace_exists",),
    text="ok quick.list {total}\nnext: {next}",
    data={
        "type": "object",
        "properties": {
            "rows": arr({"type": "object"}),
            "next_page": {"type": ["string", "null"]},
            "total": INT,
        },
        "required": ["rows", "next_page", "total"],
        "additionalProperties": False,
    },
    handler=handle,
)
