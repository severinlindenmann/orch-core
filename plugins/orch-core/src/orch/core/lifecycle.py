from __future__ import annotations

from contextvars import ContextVar

from orch.core import tasks as tk
from orch.core.constants import STATUSES
from orch.core.gates import gate_state
from orch.errors import HumanOnlyError, TransitionError, UsageError, ValidationError

HUMAN_HINT = "ask the human to do this in their own terminal or in the dashboard"


# Set only while Ops runs a dry run (#7), which writes nothing: checks inside the previewed mutation skip the harness
# check below. Each human-only Ops method checks the actor (and the harness) once before that, so a dry run inside an
# agent harness is still refused; the actor must be a human one, and any real write checks again.
PREVIEW: ContextVar[bool] = ContextVar("orch_preview", default=False)


def require_human(actor, what: str) -> None:
    if not actor.is_human:
        raise HumanOnlyError(f"{what} is a human-only action", hint=HUMAN_HINT)
    if PREVIEW.get():
        return
    # Whatever `via` says (tty, dashboard, phone), a human action is refused when an agent harness runs this
    # process: code that builds a human Actor itself gets no further than the CLI. `orch serve` passed the same check
    # when it started, so its requests pass it again (the ancestry is read once per process); there is no exemption
    # to obtain.
    from orch.actor import agent_harness
    harness = agent_harness()
    if harness:
        raise HumanOnlyError(f"{what} refused: running inside an agent harness ({harness})", hint=HUMAN_HINT)


def unanswered_blocking(ticket) -> list[dict]:
    return [
        q for q in ticket.meta.get("questions") or []
        if isinstance(q, dict) and q.get("blocking", True) and q.get("answer") in (None, "")
    ]


def _no_open_questions(ticket) -> None:
    open_qs = unanswered_blocking(ticket)
    if open_qs:
        raise ValidationError("unanswered blocking questions: " + ", ".join(str(q.get("id")) for q in open_qs))


def _tasks_closed(ticket) -> None:
    """Decision 1: every size needs at least one task, and none may be open."""
    try:
        items = tk.ticket_tasks(ticket)
    except tk.TaskParseError as e:
        raise ValidationError(e.message, hint="repair the Tasks section in the raw file") from None
    if not items:
        raise ValidationError("no tasks", hint=f"create the task list with `orch task add {ticket.id} --file tasks.yaml`, then work it down")
    open_ = tk.open_ids(items)
    if open_:
        raise ValidationError("open tasks: " + ", ".join(open_), hint=f"finish them, or `orch task skip {ticket.id} <T> -m reason`")


def check_move(ticket, to: str, actor, *, plan_skip_sizes, command: str = "move", open_blockers=()) -> None:
    """Raise unless `actor` may move `ticket` to `to` via `command` (move | claim | auto | verdict | close | reopen)."""
    frm = ticket.status
    if to not in STATUSES:
        raise UsageError(f"unknown status {to!r}", hint="one of: " + ", ".join(STATUSES))
    if frm == to:
        raise TransitionError(f"{ticket.id} is already {to}")
    if command == "close":
        require_human(actor, "closing a ticket")
        if to != "done":
            raise TransitionError("closing moves a ticket to done")
        return
    if command == "reopen":
        require_human(actor, "reopening a ticket")
        if frm != "done" or to not in ("open", "backlog"):
            raise TransitionError(f"only done tickets are reopened (to open or backlog), not {frm} → {to}")
        return
    pair = (frm, to)
    if ticket.meta.get("type") == "epic" and to in ("in-progress", "waiting", "testing"):
        raise TransitionError(f"{ticket.id} is an epic: its children are worked, it is not",
                              hint="an epic is done with one verdict once its children are in testing")

    if to == "backlog":
        require_human(actor, "moving a ticket back to backlog")
        return
    if pair == ("backlog", "open"):
        require_human(actor, "moving a ticket to open")
        state = gate_state(ticket, "requirements")
        if state != "approved":
            raise ValidationError(f"requirements gate is {state}", hint=f"orch approve {ticket.id} requirements")
        return
    if pair == ("open", "in-progress"):
        if not actor.is_human and command != "claim":
            raise TransitionError("agents start work with `orch claim`", hint=f"orch claim {ticket.id}")
        if open_blockers:
            raise ValidationError(f"blocked by unfinished tickets: {', '.join(open_blockers)}")
        return
    if pair == ("in-progress", "waiting"):
        if not unanswered_blocking(ticket):
            raise ValidationError("waiting needs an unanswered blocking question", hint=f"orch ask {ticket.id} --file questions.yaml")
        return
    if pair == ("waiting", "in-progress"):
        if command != "auto":
            require_human(actor, "resuming a waiting ticket")
        _no_open_questions(ticket)
        return
    if pair == ("in-progress", "testing"):
        if ticket.meta.get("size") not in plan_skip_sizes:
            state = gate_state(ticket, "plan")
            if state != "approved":
                raise ValidationError(f"plan gate is {state}", hint=f"the human approves with `orch approve {ticket.id} plan`")
        if not ticket.section("Verification").strip():
            raise ValidationError("Verification section is empty", hint="record the commands you ran and their evidence first")
        _tasks_closed(ticket)
        _no_open_questions(ticket)
        return
    if frm == "testing" and to in ("done", "in-progress"):
        if command != "verdict":
            raise TransitionError("testing tickets are closed or sent back with `orch verdict`", hint=f"orch verdict {ticket.id} done|follow-up")
        require_human(actor, "giving a verdict")
        return
    raise TransitionError(f"{frm} → {to} is not an allowed transition")


def allowed_targets(ticket, actor, *, plan_skip_sizes, open_blockers=()) -> list[str]:
    """Statuses `actor` could move `ticket` to with a plain move right now (verdicts are separate)."""
    out = []
    for status in STATUSES:
        if status == ticket.status:
            continue
        try:
            check_move(ticket, status, actor, plan_skip_sizes=plan_skip_sizes, command="move", open_blockers=open_blockers)
        except (TransitionError, ValidationError, UsageError):
            continue
        out.append(status)
    return out
