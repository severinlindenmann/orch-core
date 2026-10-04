"""Task-list mutations (`orch task …`, the dashboard's task buttons). Mixed into Ops: every public
method is one change of one ticket's ## Tasks section and emits one task.* event whose data never
has `from`/`to` keys (those mean a ticket status change to check.py and the metrics)."""
from __future__ import annotations

import re

from orch.clock import stamp
from orch.core import tasks as tk
from orch.core.events import read_events
from orch.core.questions import find_question
from orch.errors import ClaimError, HumanOnlyError, TransitionError, UsageError, ValidationError

WORK_STATUSES = ("in-progress", "waiting")
_STATUS_HINT = {
    "backlog": "tasks are written after `orch claim`, once the requirements are approved",
    "open": "claim it first: `orch claim {id}`",
    "testing": "it is in testing; the human sends it back with `orch verdict {id} follow-up` first",
    "done": "it is done; file a follow-up with `orch new --from {id}`",
}


def used_numbers(ws, ticket_id: str) -> set[int]:
    """Every task number a task.added event ever named for the ticket (so IDs are never reused)."""
    out: set[int] = set()
    for e in read_events(ws, ticket_id):
        if e.kind == "task.added":
            for tid in e.data.get("tasks") or []:
                m = re.fullmatch(r"T(\d+)", str(tid))
                if m:
                    out.add(int(m.group(1)))
    return out


def check_raw_tasks(current, new) -> None:
    """replace_raw: the new Tasks section must parse, and no agent task may become done by hand."""
    try:
        after = tk.ticket_tasks(new)
    except tk.TaskParseError as e:
        raise ValidationError(e.message, hint="fix the Tasks section and save again") from None
    try:
        before = tk.ticket_tasks(current)
    except tk.TaskParseError:
        before = []  # repairing a broken section must stay possible
    ticked = tk.newly_done_agent_tasks(before, after)
    if ticked:
        raise ValidationError(f"{', '.join(ticked)}: the agent's tasks are ticked by the agent with evidence",
                              hint="skip them with a why: line, or reopen them")
    was = {t.id: t for t in before}
    taken = [t.id for t in after if t.id in was and t.owner == "human" and was[t.id].owner == "agent"
             and was[t.id].state not in ("done", "skipped")]
    if taken:
        raise ValidationError(f"{', '.join(taken)}: an open agent task is taken over with orch, not by hand",
                              hint=f"orch task edit <id> {taken[0]} --owner human")


def block_doing_for(ticket, qid: str) -> str | None:
    """Decision 3: a blocking question blocks the task in progress (on: <qid>). Returns its id."""
    if ticket.status not in WORK_STATUSES:
        return None
    try:
        items = tk.ticket_tasks(ticket)
    except tk.TaskParseError:
        return None  # a broken Tasks section must not stop the question
    task = tk.doing(items)
    if task is None or task.owner == "human":
        return None  # the human's own task is not the agent's work in progress
    task.state, task.why, task.on = "blocked", f"waits for the answer to {qid}", qid
    ticket.set_section("Tasks", tk.render(items))
    return task.id


def restart_answered(ticket) -> str | None:
    """Once no blocking question is open: the first task blocked on an answered question goes back
    to doing (unless another task is doing), the others to todo. Returns the restarted id."""
    try:
        items = tk.ticket_tasks(ticket)
    except tk.TaskParseError:
        return None
    answered = {str(q.get("id", "")).upper() for q in ticket.meta.get("questions") or []
                if isinstance(q, dict) and q.get("answer") not in (None, "", [])}
    waiting = [t for t in items if t.state == "blocked" and t.on in answered]
    if not waiting:
        return None
    restarted = None
    for task in waiting:
        if restarted is None and tk.doing(items) is None:
            task.state, restarted = "doing", task.id
        else:
            task.state = "todo"
        task.why = task.on = None
    ticket.set_section("Tasks", tk.render(items))
    return restarted


class TaskOpsMixin:
    """Needs from Ops: ws, actor, _session, _log(ticket, text), _mutate(ref, kind, fn)."""

    # -- guards ------------------------------------------------------------------------

    def _task_write_allowed(self, t) -> None:
        if t.status not in WORK_STATUSES:
            hint = _STATUS_HINT.get(t.status, "").format(id=t.id) or None
            raise TransitionError(f"{t.id} is {t.status}; tasks change while it is in progress or waiting", hint=hint)
        if self.actor.is_human:
            return
        from orch.core.ops import claim_expired
        claim = t.meta.get("claim") or {}
        if claim.get("session") != self._session or claim_expired(claim, float(self.ws.config["claims"]["ttl_hours"])):
            raise ClaimError(f"{t.id} is not claimed by this session", hint=f"orch claim {t.id}")

    def _plan_approved_for_agent(self, t) -> None:
        """#9: an agent starts or finishes work only once the plan gate its ticket needs is approved, so the stop
        after writing the plan is a rule, not just a skill instruction. `task add` stays open (the list is part of
        preparing the plan); the human's own task actions are unaffected."""
        from orch.core.gates import gate_state, plan_required
        from orch.core.ledger import require_signed
        if self.actor.is_human:
            return
        if not plan_required(self.ws, t):
            require_signed(self.ws, t, ("requirements",))  # and the answers the work goes on with
            return
        state = gate_state(t, "plan")
        if state != "approved":
            raise ValidationError(f"plan gate is {state}: work starts after the human approves the plan",
                                  hint=f"stop and wait: the human approves with `orch approve {t.id} plan`"
                                       f" (then `orch wait {t.id}`)")
        require_signed(self.ws, t, ("requirements", "plan"))

    def _may_touch(self, task) -> None:
        if not self.actor.is_human and task.owner == "human":
            raise HumanOnlyError(f"{task.id} is the human's task", hint="tell the user it waits for them")

    def _edit_tasks(self, ref: str, kind: str, fn):
        def outer(t):
            self._task_write_allowed(t)
            items = tk.ticket_tasks(t)
            data = fn(t, items)
            t.set_section("Tasks", tk.render(items))
            return data
        return self._mutate(ref, kind, outer)

    def _check_needs(self, items, ids) -> None:
        unknown, cycle = tk.needs_problems(items)
        unknown = [(a, b) for a, b in unknown if a in ids]
        if unknown:
            raise ValidationError("needs names unknown tasks: " + ", ".join(f"{a} needs {b}" for a, b in unknown))
        if cycle:
            raise ValidationError("needs form a cycle: " + " → ".join(cycle))

    # -- add / edit / import -------------------------------------------------------------

    def task_add(self, ref: str, raw_items: list, *, after: str | None = None):
        if not raw_items:
            raise UsageError("nothing to add", hint='orch task add <id> "text" or --file tasks.yaml')
        added: list[str] = []

        def fn(t, items):
            new = tk.build(raw_items, tk.next_number(items, used_numbers(self.ws, t.id)))
            after_approval = bool(((t.meta.get("gates") or {}).get("plan") or {}).get("approved"))
            if after_approval:
                for n in new:
                    n.added = f"{stamp()} {tk.AFTER_APPROVAL}"
            pos = items.index(tk.find(items, after)) + 1 if after is not None else len(items)
            items[pos:pos] = new
            self._check_needs(items, {n.id for n in new})
            added.extend(n.id for n in new)
            self._log(t, "added " + ", ".join(added) + (" after plan approval" if after_approval else ""))
            return {"tasks": list(added), "after_approval": after_approval}

        return self._edit_tasks(ref, "task.added", fn), added

    def task_edit(self, ref: str, task_id: str, *, text=None, add_refs=(), drop_refs=(), verify=None,
                  clear_verify=False, needs=None, clear_needs=False, owner=None):
        if not any((text is not None, add_refs, drop_refs, verify is not None, clear_verify,
                    needs is not None, clear_needs, owner is not None)):
            raise UsageError("nothing to change", hint="pass --text, --ref, --drop-ref, --verify, --needs or --owner")

        def fn(t, items):
            task = tk.find(items, task_id)
            self._may_touch(task)
            if task.closed:
                raise ValidationError(f"{task.id} is {task.state}; reopen it before editing",
                                      hint=f"orch task reopen {t.id} {task.id}")
            fields: list[str] = []
            try:
                if text is not None:
                    if not tk.one_line(text):
                        raise UsageError("text must not be empty")
                    task.text = tk.one_line(text)
                    fields.append("text")
                if add_refs or drop_refs:
                    drop = {(r.kind, r.target) for r in map(tk.parse_ref, drop_refs)}
                    missing = drop - {(r.kind, r.target) for r in task.refs}
                    if missing:
                        raise UsageError(f"{task.id} has no ref " + ", ".join(f"{k}:{v}" for k, v in sorted(missing)))
                    task.refs = [r for r in task.refs if (r.kind, r.target) not in drop] + [tk.parse_ref(r) for r in add_refs]
                    fields.append("ref")
                if verify is not None or clear_verify:
                    task.verify = None if clear_verify else (tk.one_line(verify) or None)
                    fields.append("verify")
                if needs is not None or clear_needs:
                    task.needs = [] if clear_needs else [tk.normalize_task_id(x) for x in needs]
                    fields.append("needs")
            except ValueError as e:
                raise UsageError(str(e)) from None
            if owner is not None:
                if owner not in tk.OWNERS:
                    raise UsageError("owner is agent or human")
                if not self.actor.is_human:
                    raise HumanOnlyError("only the human changes who owns a task")
                taken_over = task.owner == "agent" and owner == "human"  # a deliberate human takeover
                task.owner = owner
                fields.append("owner")
            else:
                taken_over = False
            self._check_needs(items, {task.id})
            if taken_over and fields == ["owner"]:
                self._log(t, f"{task.id} taken over by you")
            else:
                self._log(t, f"edited {task.id} ({', '.join(fields)})" + (f"; {task.id} taken over by you" if taken_over else ""))
            return {"task": task.id, "fields": fields}

        return self._edit_tasks(ref, "task.edited", fn)

    def task_import(self, ref: str):
        added: list[str] = []

        def fn(t, items):
            if items:
                raise ValidationError(f"{t.id} already has tasks; import works on an empty task list")
            new = tk.from_plan(t.section("Plan"), list((self.ws.config["git"].get("repos") or {}).keys()))
            if not new:
                raise ValidationError("the Plan has no checklist (- [ ] / - [x]) to import")
            start = tk.next_number([], used_numbers(self.ws, t.id))
            for i, n in enumerate(new):
                n.id = f"T{start + i}"
            items.extend(new)
            added.extend(n.id for n in new)
            self._log(t, f"imported {len(new)} tasks from the Plan")
            return {"tasks": list(added), "after_approval": False, "imported": True}

        return self._edit_tasks(ref, "task.added", fn), added

    # -- state moves -----------------------------------------------------------------------

    def _task_move(self, ref: str, task_id: str, change):
        def fn(t, items):
            task = tk.find(items, task_id)
            self._may_touch(task)
            was = task.state
            text, extra = change(t, items, task)
            self._log(t, text)
            return {"task": task.id, "was": was, "now": task.state, **extra}
        return self._edit_tasks(ref, "task.moved", fn)

    def task_start(self, ref: str, task_id: str):
        def change(t, items, task):
            self._plan_approved_for_agent(t)
            if task.state == "doing":
                raise ValidationError(f"{task.id} is already in progress")
            if task.closed:
                raise ValidationError(f"{task.id} is {task.state}; reopen it first", hint=f"orch task reopen {t.id} {task.id}")
            busy = tk.doing(items)
            if busy:
                raise ValidationError(f"{busy.id} is in progress: done, block or skip it first")
            waiting = tk.needs_open(task, items)
            if waiting:
                raise ValidationError(f"{task.id} needs {', '.join(waiting)} closed first")
            task.state, task.why, task.on = "doing", None, None
            return f"started {task.id}", {}
        return self._task_move(ref, task_id, change)

    def task_done(self, ref: str, task_id: str, note: str | None = None):
        note = tk.one_line(note) or None

        def change(t, items, task):
            self._plan_approved_for_agent(t)
            if self.actor.is_human and task.owner != "human":
                raise TransitionError(f"{task.id} is the agent's task; the agent ticks it with evidence",
                                      hint="skip it with a reason, or reopen it")
            if task.state not in ("todo", "doing"):
                raise ValidationError(f"{task.id} is {task.state}", hint=f"orch task reopen {t.id} {task.id}")
            waiting = tk.needs_open(task, items)
            if waiting:
                raise ValidationError(f"{task.id} needs {', '.join(waiting)} closed first")
            if task.verify and not note:
                raise UsageError(f"{task.id} has a verify line: say what proved it with -m",
                                 hint=f'orch task done {t.id} {task.id} -m "…"')
            started = task.state == "doing"
            task.state, task.why, task.on = "done", None, None
            if note:
                task.note = note
            text = f"{task.id} done" + (f": {note}" if note else "") + ("" if started or task.owner == "human" else " (done without start)")
            return text, ({"note": note} if note else {})
        return self._task_move(ref, task_id, change)

    def task_skip(self, ref: str, task_id: str, reason: str):
        reason = tk.one_line(reason)
        if not reason:
            raise UsageError("say why with -m", hint=f'orch task skip {ref} {task_id} -m "reason"')

        def change(t, items, task):
            if task.closed:
                raise ValidationError(f"{task.id} is already {task.state}")
            task.state, task.why, task.on = "skipped", reason, None
            return f"skipped {task.id}: {reason}", {"why": reason}
        return self._task_move(ref, task_id, change)

    def task_block(self, ref: str, task_id: str, reason: str, on: str | None = None):
        reason = tk.one_line(reason)
        if not reason:
            raise UsageError("say why with -m", hint=f'orch task block {ref} {task_id} -m "reason" --on Q3')
        try:
            target = tk.normalize_on(on) if on else None
        except ValueError as e:
            raise UsageError(str(e)) from None

        def change(t, items, task):
            if task.closed:
                raise ValidationError(f"{task.id} is {task.state}; reopen it first")
            if target and tk.on_kind(target) == "question":
                find_question(t, target)  # NotFoundError (exit 2) for an unknown question
            if target and tk.on_kind(target) == "task":
                if target == task.id:
                    raise UsageError("a task cannot wait for itself")
                tk.find(items, target)
            task.state, task.why, task.on = "blocked", reason, target
            text = f"blocked {task.id}" + (f" on {target}" if target else "") + f": {reason}"
            return text, {"why": reason, "on": target}
        return self._task_move(ref, task_id, change)

    def task_reopen(self, ref: str, task_id: str, reason: str | None = None):
        reason = tk.one_line(reason) or None

        def change(t, items, task):
            if task.state in ("todo", "doing"):
                raise ValidationError(f"{task.id} is {task.state}; only done, skipped or blocked tasks reopen")
            task.state, task.why, task.on = "todo", None, None
            return f"reopened {task.id}" + (f": {reason}" if reason else ""), ({"why": reason} if reason else {})
        return self._task_move(ref, task_id, change)
