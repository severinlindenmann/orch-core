"""``quick.list`` on the workspace socket (H11f, #308, D-P2-35; contract ``quick/1``): the quick tasks of a viewer, each
with how many commits and files its branch changes.

A quick task is a ticket (``type: chore``, label ``quick``): this op is ``ticket.query {label:["quick"]}`` for the same
viewer (restricted tickets are never listed or counted, a later page rebuilds from the current state) plus, per row, the
counts of the ticket's linked branches. There is no quick-task store and no quick-task event, and the limits
(``max_commits``, ``max_files``) are arguments of the read: they decide ``over`` and nothing else.

Three phases, as ``diff`` (:mod:`orch.host.diff`), because the counts are git reads:

1. **On the workspace worker thread** (:func:`prepare`): the rows through :func:`orch.host.reads_query.ticket_query`
   (filter first, then count), and per row the repos and branches from the store (:func:`orch.ops.diff_core.diff_plan`);
   a row without a linked repo or a readable plan has unknown counts. Nothing is read from git here.
2. **Off the worker thread** (:func:`run`, on :data:`orch.host.diff.EXECUTOR`): one reader call per row, within
   :data:`BUDGET_S` for all rows together. A row whose reader fails, or that comes after the budget, has ``null``
   counts: never an error, the numbers are a display.
3. **Back on the worker thread** (:func:`finish`): the column and the visibility again, from the refreshed store; a row
   whose ticket the viewer cannot see any more is dropped (and not counted).

The answer is the one the direct-mode op builds (:mod:`orch.ops.commands.quick_list`), plus the full
``ticket.query`` row fields.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch.ops import quick as core

from .reads import ReadCtx
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["BUDGET_S", "SCHEMA", "VALIDATOR", "Pending", "finish", "prepare", "run"]

LOG = logging.getLogger(__name__)
#: all rows' reader calls together; a row that does not get its turn within it has unknown counts
BUDGET_S = core.BUDGET_S
_STATUS = {"enum": ["backlog", "open", "in_progress", "waiting", "testing", "done", "closed"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "array", "minItems": 1, "maxItems": 8, "items": _STATUS},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "page": {"type": "string", "minLength": 1, "maxLength": 512},
        "max_commits": {"type": "integer", "minimum": 1, "maximum": 100},
        "max_files": {"type": "integer", "minimum": 1, "maximum": 1000},
    },
    "additionalProperties": False,
}
VALIDATOR = Draft202012Validator(SCHEMA)


@dataclass(frozen=True)
class Pending:
    """What the worker thread resolved: the reader phase needs neither the store nor the call."""

    principal: str
    caps: Any
    data: dict[str, Any]
    #: row uid -> the repos to read (absent: unknown counts)
    jobs: dict[str, list[Any]]
    max_commits: int
    max_files: int


def prepare(call: Call, args: dict[str, Any]) -> Pending:
    """Phase 1, on the worker thread (args already passed :data:`SCHEMA`)."""
    from orch.ops import addon_gate
    from orch.ops.diff_core import diff_plan
    from orch.ops.errors import OrchError

    from .errors import HostError
    from .reads_query import ticket_query

    # the host serves ``quick.list`` itself (not through the registry handler), so the availability check of
    # ``operation(addon="quick")`` is made here, with the same code and words
    if not addon_gate.is_on(call.service.store.state.workspace.addons, core.ADDON):
        raise HostError("addon.off", addon_gate.off_message(core.ADDON))

    query: dict[str, Any] = {"label": [core.LABEL], "sort": "key", "order": "desc", "limit": args.get("limit", 100)}
    for k in ("status", "page"):
        if k in args:
            query[k] = args[k]
    ctx = ReadCtx(call, Viewer.of(call), query)
    data = ticket_query(ctx)  # filter first, then count: restricted rows are never listed or counted
    service = call.service
    caps = service.caps_for(call.principal)  # pins kept by the service; reader children under the caller (F10)
    tickets = service.store.state.tickets
    jobs: dict[str, list[Any]] = {}
    for row in data["rows"]:
        view = tickets.get(row["uid"])
        if view is None or not view.fields["links"]["repos"]:
            continue
        try:
            jobs[row["uid"]] = diff_plan(service.store, view, caps)
        except OrchError:  # no branch, no working copy: unknown counts
            continue
    return Pending(
        call.principal,
        caps,
        data,
        jobs,
        args.get("max_commits", core.DEFAULT_MAX_COMMITS),
        args.get("max_files", core.DEFAULT_MAX_FILES),
    )


def run(pending: Pending) -> dict[str, Any]:
    """Phase 2, off the worker thread: the counts, within :data:`BUDGET_S`."""
    from orch.ops.diff_core import RepoReadError, diff_run
    from orch.ops.errors import OrchError

    deadline = time.monotonic() + BUDGET_S
    counts: dict[str, tuple[int | None, int | None]] = {}
    for row in pending.data["rows"]:
        jobs = pending.jobs.get(row["uid"])
        if not jobs:
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            counts[row["uid"]] = core.counts(diff_run(pending.caps, jobs, context=0, max_bytes=1024, budget=left))
        except (RepoReadError, OrchError) as e:
            LOG.info("quick.list: %s: %s (%s)", row["key"], e, pending.principal)
    rows = []
    for row in pending.data["rows"]:
        commits, files = counts.get(row["uid"], (None, None))
        over = core.over(commits, files, pending.max_commits, pending.max_files)
        rows.append({**row, "quick": {"commits": commits, "files": files, "over": over}})
    return {"rows": rows, "next_page": pending.data["next_page"], "total": pending.data["total"]}


def finish(call: Call, data: dict[str, Any]) -> dict[str, Any]:
    """Phase 3, on the worker thread: the column and the viewer again (F8), rows whose ticket is no longer visible
    dropped, and the text lines both modes render."""
    call.authorize()
    viewer = Viewer.of(call)
    tickets = call.service.store.state.tickets
    rows = [r for r in data["rows"] if (v := tickets.get(r["uid"])) is not None and viewer.sees(v)]
    total = data["total"] - (len(data["rows"]) - len(rows))
    lines, hints = core.lines(rows, total, data["next_page"])
    return {"data": {"rows": rows, "next_page": data["next_page"], "total": total}, "lines": lines, "hints": hints}
