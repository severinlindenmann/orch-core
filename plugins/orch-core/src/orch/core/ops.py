from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from orch.clock import now, parse_stamp, stamp
from orch.core import evidence, store, trackers
from orch.core.constants import PRIORITIES, RESOLUTIONS, SECTIONS, SIZES, STATUSES, SUCCEEDED, TYPES
from orch.core.events import Actor, Event, append_event, log_line
from orch.core.gates import (GATE_SECTIONS, HASH_VERSION, REAPPROVE_IN_PLACE, clear_gate, gate_hash, gate_state, human_questions_in,
                             record_approval, requirements_required, requirements_skip_sizes)
from orch.core.ids import next_id
from orch.core.lifecycle import HUMAN_HINT, check_move, require_human, unanswered_blocking
from orch.core.locks import lock
from orch.core.model import Ticket, new_ticket, parse_ticket
from orch.core.ops_tasks import TaskOpsMixin, block_doing_for, check_raw_tasks, restart_answered
from orch.core.protect import agent_wrote_ask, protected_changes
from orch.core.questions import build_questions, find_question, question_hash, validate_answer
from orch.errors import ClaimError, HumanOnlyError, OrchError, TransitionError, UsageError, ValidationError

_UNSAFE_NAME = re.compile(r'[\\:*?"<>|]')
_EMPTY_CLAIM = {"session": None, "harness": None, "at": None}
PR_STATES = ("open", "merged", "declined", "draft")  # what `orch link --state` records; new links start "unknown"


def claim_expired(claim: dict, ttl_hours: float) -> bool:
    at = claim.get("at")
    if not at:
        return True
    try:
        return (now() - parse_stamp(str(at))).total_seconds() > ttl_hours * 3600
    except ValueError:
        return True


def _choice(field: str, value: str, allowed: tuple) -> None:
    if value not in allowed:
        raise UsageError(f"{field} must be one of {', '.join(allowed)}, got {value!r}")


MAX_LABEL = 64


def check_labels(names) -> list[str]:
    """Label names as given, in order, each once (#173). A label is one word of up to MAX_LABEL characters: no
    whitespace, no comma and no hidden characters, so it reads the same everywhere (`customer:arbonia`, `admin`)."""
    from orch.textsafe import is_hidden
    out: list[str] = []
    for raw in names:
        name = str(raw)
        if not name:
            raise UsageError("a label must not be empty")
        if any(c.isspace() or c == "," or is_hidden(c) for c in name):
            raise UsageError(f"label {name!r} holds whitespace, a comma or a hidden character",
                             hint="a label is one word, e.g. customer:arbonia or admin; pass several labels separately")
        if len(name) > MAX_LABEL:
            raise UsageError(f"label {name[:20]!r}... is longer than {MAX_LABEL} characters")
        if name not in out:
            out.append(name)
    return out


def _current_labels(t: Ticket) -> list[str]:
    labels = t.meta.get("labels")
    if isinstance(labels, str):  # a hand-written `labels: admin`
        return [labels]
    return [str(x) for x in labels] if isinstance(labels, list) else []


def _refuse_hidden(what: str, *texts) -> None:
    """A human decision is refused while the text it binds holds hidden characters (orch.textsafe): controls, bidi,
    zero-width and similar could make the text read differently from what is signed."""
    from orch.textsafe import decodes_to_hidden
    if any(decodes_to_hidden(x) for x in texts):
        raise ValidationError(f"cannot decide: the {what} holds hidden or control characters",
                              hint="request changes so the agent removes the hidden characters")


def _require_seen(expected_hash, what: str) -> None:
    """Every human decision is bound to the hash of what the human read (gate text, question, criteria and
    evidence): without one nothing is applied."""
    if not expected_hash:
        raise ValidationError(f"{what} needs the hash of what you read; nothing was applied",
                              hint="reload the page (or update the app that sent it) and review again")


def _refuse_changed_gates(t) -> None:
    """A done verdict is refused while an approval it rests on covers text that changed since (send back stays)."""
    from orch.core.gates import invalidated_gates
    changed = invalidated_gates(t)
    if changed:
        raise ValidationError(f"the {' and '.join(changed)} of {t.id} changed since approval: re-approve or send "
                              "back first", hint=f"re-approve it with `orch approve {t.id} {changed[0]}`, or send {t.id} back with a follow-up")


def _check_section_text(name: str, text: str) -> None:
    """Every section text orch writes is refused when it would not read back as that one section (model)."""
    from orch.core.model import section_text_problem
    problem = section_text_problem(text)
    if problem:
        raise ValidationError(f"cannot write {name}: {problem}",
                              hint="use `###` for headings inside a section, and close every ``` or ~~~ fence")


def _skip_open_tasks(t: Ticket, reason: str) -> list[str]:
    """Close: every open task (todo, doing, blocked) becomes skipped with the close reason as its why, as a human
    skip would do, so a closed ticket has no open tasks left. A broken Tasks section is left as it is."""
    from orch.core import tasks as tk
    try:
        items = tk.ticket_tasks(t)
    except tk.TaskParseError:
        return []
    why = tk.one_line(reason)
    skipped = []
    for task in items:
        if task.state in tk.OPEN_STATES:
            task.state, task.why, task.on = "skipped", why, None
            skipped.append(task.id)
    if skipped:
        t.set_section("Tasks", tk.render(items))
    return skipped


def file_stamp(path) -> tuple | None:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class Ops(TaskOpsMixin):
    """The single write path. Every public method is one mutation and emits one event."""

    def __init__(self, ws, actor: Actor, *, dry_run: bool = False):
        self.ws = ws
        self._actor = actor
        self.notices: list[str] = []  # what the last operation chose for the caller (`new --from` joining an epic)
        self.warnings: list[str] = []  # what the last operation allowed but the caller should hear about
        # dry_run (#7): every check runs as usual on the loaded ticket and the method returns the ticket as it would
        # be, but nothing is written: no lock file, no save, no event, no ledger entry, no gate snapshot.
        self.dry_run = dry_run
        # The ticket file's (mtime_ns, size, inode) as a preview saw it: checked inside the lock before a write, so a
        # human-only CLI command applies only to the state it showed (verdict and move have no gate hash to bind).
        self.expected_stamp: tuple | None = None

    @property
    def actor(self) -> Actor:
        return self._actor

    # -- internals -----------------------------------------------------------------

    @property
    def _skip_sizes(self) -> tuple:
        return tuple(self.ws.config["gates"]["plan_skip_sizes"])

    @property
    def _req_skip_sizes(self) -> tuple:
        return requirements_skip_sizes(self.ws)

    @property
    def _session(self) -> str:
        return self.actor.session or "local"

    def _binding(self) -> dict | None:
        """The runner's binding of this agent session (the same trusted binding the permission hook uses: runner-written,
        and only for a process under the one it started), or None when the session certainly has none: then nothing
        here changes. Fails closed: a binding record that does not verify, or an error while looking, refuses."""
        if self.actor.is_human:
            return None
        from orch.core import factory_sessions
        state, b = factory_sessions.session_state(self.ws, self.actor.session)
        if state == "unknown":
            raise ValidationError("orch cannot tell whether this session is an AI Factory session (its binding does not "
                                  "verify): nothing was changed", hint="stop and tell the human")
        return b

    def _bound_epic(self) -> str | None:
        b = self._binding()
        return b["epic"] if b else None

    def _in_bound_epic(self, t: Ticket, what: str) -> None:
        """The one scope check of a factory session, for every change to an existing ticket (`_mutate`): its own epic,
        that epic's children (their parent named by ticket id), or a ticket the session itself created (its
        follow-ups: factory_sessions.created, noted by `new`). Nothing else: not a ticket the human or another session
        filed during the run, not one whose parent does not resolve."""
        b = self._binding()
        if b is None:
            return
        bound = b["epic"].upper()
        if t.id.upper() == bound:
            return
        from orch.core import factory_sessions
        from orch.core.epics import parent_epic
        parent = parent_epic(self.ws, t)
        if parent is not None and parent.id.upper() == bound:
            return
        if factory_sessions.created(b["session"], t.id):
            return
        raise ValidationError(f"{what} {t.id} refused: this AI Factory session works on epic {b['epic']}, its children "
                              "and the tickets it created itself", hint=f"orch epic show {b['epic']}")

    def _bound_target(self, epic_ref: str | None, what: str) -> None:
        """`--epic X` from a factory session names its own epic."""
        if epic_ref is None:
            return
        bound = self._bound_epic()
        if bound is not None and store.resolve(self.ws, epic_ref).id.upper() != bound.upper():
            raise ValidationError(f"{what} refused: this AI Factory session works on epic {bound} only",
                                  hint=f"orch new --epic {bound} ...")

    def _emit(self, ticket_id: str | None, kind: str, data: dict | None = None) -> Event | None:
        if self.dry_run:
            return None
        event = append_event(self.ws, ticket_id, kind, self.actor, data)
        return event

    def _log(self, ticket: Ticket, text: str) -> None:
        # One Log entry is always one line: any embedded newline (a multi-line request_changes/verdict message,
        # an answer's note, ...) is flattened to a space so it can never forge a heading or fence on re-parse.
        ticket.append_log(log_line(self.actor, " ".join(str(text).split())))

    def _mutate(self, ref: str, kind: str, fn: Callable[[Ticket], dict | None]) -> Ticket:
        entry = store.resolve(self.ws, ref)
        if self.dry_run:
            from orch.core.lifecycle import PREVIEW
            token = PREVIEW.set(True)
            try:
                _, ticket = store.load(self.ws, entry.id)
                self._in_bound_epic(ticket, "changing")
                fn(ticket)
            finally:
                PREVIEW.reset(token)
            return ticket
        with lock(self.ws, entry.id):
            if self.expected_stamp is not None and file_stamp(entry.path) != self.expected_stamp:
                raise ValidationError(f"{entry.id} changed since you reviewed it; nothing was applied",
                                      hint="look at it again and repeat the command")
            path, ticket = store.load(self.ws, entry.id)  # raises TicketParseError before any write
            _record_owed_invalidations(self.ws, ticket)  # the audit trail first: reads never write this (#108)
            self._in_bound_epic(ticket, "changing")  # every change to an existing ticket: one scope check
            before = ticket.status
            data = fn(ticket) or {}
            if ticket.status != before:  # one uniform shape for every status change
                data["from"], data["to"] = before, ticket.status
            store.save(self.ws, ticket, path)
        self._emit(ticket.id, kind, data)
        return ticket

    def _mutate_events(self, ref: str, fn: Callable[[Ticket], list[tuple[str, dict]]]) -> Ticket:
        """`_mutate` for an operation that records more than one event (an epic approval per child gate):
        `fn` returns [(kind, data), ...]; the first one carries the status change."""
        entry = store.resolve(self.ws, ref)
        if self.dry_run:
            from orch.core.lifecycle import PREVIEW
            token = PREVIEW.set(True)
            try:
                _, ticket = store.load(self.ws, entry.id)
                self._in_bound_epic(ticket, "changing")
                fn(ticket)
            finally:
                PREVIEW.reset(token)
            return ticket
        with lock(self.ws, entry.id):
            path, ticket = store.load(self.ws, entry.id)
            _record_owed_invalidations(self.ws, ticket)
            self._in_bound_epic(ticket, "changing")
            before = ticket.status
            records = fn(ticket) or []
            if records and ticket.status != before:
                records[0][1]["from"], records[0][1]["to"] = before, ticket.status
            store.save(self.ws, ticket, path)
        for kind, data in records:
            self._emit(ticket.id, kind, data)
        return ticket

    def _ledger(self, t: Ticket, kind: str, **fields) -> None:
        """Sign a human decision into the approval ledger (orch.core.ledger), before the ticket is saved."""
        if self.dry_run:
            return
        from orch.actor import process_evidence
        from orch.core import ledger
        record = ledger.record_status if kind in ledger.STATUS_KINDS else ledger.record
        record(self.ws, ticket=t.id, kind=kind, actor=self.actor, evidence=process_evidence(), **fields)

    def _external(self, key: str) -> dict:
        return trackers.external_ref(self.ws.config["external_trackers"], key)

    def _open_blockers(self, ticket: Ticket) -> list[str]:
        from orch.core.query import open_blockers
        return open_blockers(self.ws, ticket)

    # -- create ----------------------------------------------------------------------

    def new(self, title: str, *, type: str = "feature", priority: str = "normal", size: str = "m",
            ask: str = "", external: str | None = None, from_ref: str | None = None, epic: str | None = None,
            sprint: str | None = None, sections: dict[str, str] | None = None, due: str | None = None,
            labels: list[str] | None = None, no_epic: bool = False) -> Ticket:
        """`sections` (#24): Summary, Requirements, Acceptance criteria and Out of scope written at creation, with the
        same rules as `orch section set`. `labels` (#173): checked by `check_labels`. `self.warnings` names a gated
        section left empty. `from_ref` with `epic` (#213): `parent` is the epic, the source keeps its `follow_ups`
        back-link; `from_ref` alone joins the source's epic (not done, new type not epic) unless `no_epic`, and
        says so in `self.notices`."""
        from orch.core.body import BODY_SECTIONS, empty_gate_warnings, tasks_from_block
        self.warnings = []
        self.notices = []
        bound = self._binding()  # first: refuses while orch cannot tell whether this is a factory session
        title = " ".join(title.split())
        if not title:
            raise UsageError("title must not be empty")
        _choice("type", type, TYPES)
        _choice("priority", priority, PRIORITIES)
        _choice("size", size, SIZES)
        if epic and no_epic:
            raise UsageError("pass --epic or --no-epic, not both")
        self._bound_target(epic, "creating a child")
        source = store.resolve(self.ws, from_ref) if from_ref else None  # validate before allocating an ID
        if source is not None:
            self._in_bound_epic(store.load(self.ws, source.id)[1], "following up")
        parent_epic = self._epic_target(epic, child_type=type) if epic else None
        if source and not epic and not no_epic and type != "epic":
            from orch.core.epics import parent_epic as epic_of
            inherited = epic_of(self.ws, source)
            if inherited is not None and inherited.status != "done":
                parent_epic = store.resolve(self.ws, inherited.id)
                self.notices.append(f"created in epic {parent_epic.id} (from {source.id}'s epic)")
        sprint_id = self._sprint(sprint) if sprint else None
        if due is not None:
            from orch.core.due import checked_due
            due = checked_due(due)
        labels = check_labels(labels or [])
        sections = sections or {}
        unknown = [n for n in sections if n not in BODY_SECTIONS]
        if unknown:
            raise UsageError(f"orch new does not fill {unknown[0]!r}", hint="one of: " + ", ".join(BODY_SECTIONS))
        sections = dict(sections)
        raw_tasks = sections.pop("Tasks", None)  # YAML, as `orch task add --file` reads it (#216)
        new_tasks = None
        if raw_tasks is not None:
            if not raw_tasks.strip():
                raw_tasks = None
            else:
                from orch.core import tasks as tk
                new_tasks = tk.build(tasks_from_block(raw_tasks), 1)
                self._check_needs(new_tasks, {n.id for n in new_tasks})
        if sections.get("Plan") and type == "epic":
            raise UsageError("an epic has no plan of its own", hint="its children have plans")
        for name, text in [("Ask", ask), *sections.items()]:
            _check_section_text(name, text)
        unproven = evidence.ticked_without_evidence(Ticket(meta={}, sections=dict(sections)))
        if unproven:
            raise ValidationError("cannot tick " + ", ".join(f"AC{n}" for n in unproven) + " without evidence",
                                  hint="create the ticket with unticked criteria (`- [ ]`)")
        tid = next_id(self.ws)
        ticket = new_ticket(tid, title, type=type, priority=priority, size=size, created=stamp())
        ticket.set_section("Ask", ask)
        for name, text in sections.items():
            ticket.set_section(name, text)
        if new_tasks:
            from orch.core import tasks as tk
            ticket.set_section("Tasks", tk.render(new_tasks))
        if external:
            ticket.meta["external"].append(self._external(external))
        if source:
            ticket.meta["parent"] = source.id
            ticket.set_section("Context", f"Follow-up from {source.id}.")
        if parent_epic:
            ticket.meta["parent"] = parent_epic.id
        if sprint_id:
            ticket.meta["sprint"] = sprint_id
        if due:
            ticket.meta["due"] = due
        if labels:
            ticket.meta["labels"] = labels
        self._log(ticket, "created" + (f" in epic {parent_epic.id}" if parent_epic else ""))
        with lock(self.ws, tid):
            store.save(self.ws, ticket)
            ticket = store.load(self.ws, tid)[1]  # the ticket as saved: what is returned and warned about is on disk
        if bound is not None and not self.dry_run:
            from orch.core import factory_sessions
            factory_sessions.record_created(bound["session"], tid)  # its own ticket: it may go on refining it
        self._emit(tid, "ticket.created", {"title": title, "from": source.id if source else None,
                                           **({"epic": parent_epic.id} if parent_epic else {}),
                                           **({"due": due} if due else {}),
                                           **({"labels": labels} if labels else {}),
                                           **({"tasks": [n.id for n in new_tasks]} if new_tasks else {}),
                                           **({"external": ticket.meta["external"][0]["key"]} if external else {})})
        for n in new_tasks or []:  # one task.added per task, shaped as `orch task add` records it (#216)
            self._emit(tid, "task.added", {"tasks": [n.id], "after_approval": False})
        if source:
            def link_back(t: Ticket) -> dict:
                t.meta.setdefault("follow_ups", []).append(tid)
                self._log(t, f"follow-up {tid} created")
                return {"follow_up": tid}
            self._mutate(source.id, "ticket.edited", link_back)
        self.warnings = empty_gate_warnings(ticket) if requirements_required(self.ws, ticket) else []
        return ticket

    def _epic_target(self, ref: str, *, child_type: str) -> store.Entry:
        """The epic a ticket is created in or linked to: it must be an epic and not done, and the child is not one."""
        from orch.core.epics import is_epic
        if child_type == "epic":
            raise ValidationError("no nested epics: an epic cannot be the child of another epic")
        target = store.resolve(self.ws, ref)
        if not is_epic(target.meta):
            raise ValidationError(f"{target.id} is not an epic", hint=f"create one with `orch new --type epic`")
        if target.status == "done":
            raise ValidationError(f"{target.id} is done; it takes no more children")
        return target

    def _sprint(self, ref: str) -> str:
        from orch.core.sprints import find
        return find(self.ws, ref)["id"]

    # -- claims ----------------------------------------------------------------------

    def claim(self, ref: str) -> Ticket:
        session = self._session
        ttl = float(self.ws.config["claims"]["ttl_hours"])

        def fn(t: Ticket) -> dict:
            from orch.core.query import claim_is_expired
            if t.meta.get("type") == "epic":
                raise TransitionError(f"{t.id} is an epic: claim one of its children instead",
                                      hint=f"orch list, then orch claim <child of {t.id}>")
            if t.status not in ("open", "in-progress"):
                raise TransitionError(f"{t.id} is {t.status}; only open or in-progress tickets can be claimed")
            current = t.meta.get("claim") or {}
            if current.get("session") and current["session"] != session \
                    and not claim_is_expired(self.ws, t.id, t.status, current):
                raise ClaimError(
                    f"{t.id} is claimed by {current.get('harness')} (session {str(current['session'])[:8]}) since {current.get('at')}",
                    hint=f"claims expire {ttl:g} h after their last sign of life; the holder can run `orch release {t.id}`",
                )
            frm = t.status
            if not self.actor.is_human:
                from orch.core.ledger import require_signed
                from orch.core.permits import require_budget
                require_signed(self.ws, t, ("requirements",))
                require_budget(self.ws, t)
            if frm == "open":
                check_move(t, "in-progress", self.actor, plan_skip_sizes=self._skip_sizes,
                           command="claim", open_blockers=self._open_blockers(t))
                t.meta["status"] = "in-progress"
            t.meta["claim"] = {"session": session, "harness": self.actor.name, "at": stamp()}
            sessions = t.meta.setdefault("sessions", [])
            if not any(s.get("id") == session for s in sessions):
                sessions.append({"id": session, "harness": self.actor.name,
                                 "model": os.environ.get("ORCH_MODEL"), "started": stamp()})
            self._log(t, "claimed" + (" (open → in-progress)" if frm == "open" else ""))
            return {"session": session}

        return self._mutate(ref, "claim.taken", fn)

    def release(self, ref: str) -> Ticket:
        session = self._session

        def fn(t: Ticket) -> dict:
            current = t.meta.get("claim") or {}
            if not current.get("session"):
                raise ValidationError(f"{t.id} is not claimed")
            if current["session"] != session and not self.actor.is_human:
                raise ClaimError(f"{t.id} is claimed by another session ({str(current['session'])[:8]})")
            t.meta["claim"] = dict(_EMPTY_CLAIM)
            self._log(t, "released claim")
            return {"session": current["session"]}

        return self._mutate(ref, "claim.released", fn)

    # -- content ---------------------------------------------------------------------

    def log(self, ref: str, text: str) -> Ticket:
        text = " ".join(text.split())
        if not text:
            raise UsageError("log message must not be empty")

        def fn(t: Ticket) -> dict:
            self._log(t, text)
            return {"text": text}

        return self._mutate(ref, "log.added", fn)

    def set_state(self, ref: str, text: str) -> Ticket:
        _check_section_text("Current state", text)

        def fn(t: Ticket) -> dict:
            t.set_section("Current state", text)
            self._log(t, "updated current state")
            return {}

        return self._mutate(ref, "state.updated", fn)

    def set_section(self, ref: str, name: str, text: str) -> Ticket:
        return self._write_section(ref, name, lambda current: text)

    def append_section(self, ref: str, name: str, text: str) -> Ticket:
        """Add `text` (e.g. an ```orch block) after what the section holds, read and written under the ticket's lock,
        with the same rules and event as `set_section`."""
        return self._write_section(ref, name, lambda current: f"{current}\n\n{text}" if current.strip() else text)

    def _section_name(self, name: str) -> str:
        canonical = {s.lower(): s for s in SECTIONS}.get(name.strip().lower())
        if canonical is None:
            raise UsageError(f"unknown section {name!r}", hint="one of: " + ", ".join(SECTIONS))
        if canonical == "Log":
            raise UsageError("the Log is append-only", hint="use `orch log`")
        if canonical == "Tasks":
            raise UsageError("the Tasks section changes only through `orch task`",
                             hint="orch task add | edit | start | done | skip | block | reopen")
        return canonical

    def _put_section(self, t: Ticket, canonical: str, text: str) -> None:
        """The rules of every section write, shared by one section and several: text that reads back as one
        section, the Ask's human-only rule (#24), no plan on an epic."""
        _check_section_text(canonical, text)
        if canonical == "Ask" and (self.actor.is_human or not agent_wrote_ask(self.ws, t)):  # #24
            require_human(self.actor, "editing the Ask section")
        if canonical == "Plan" and t.meta.get("type") == "epic":
            raise UsageError(f"{t.id} is an epic: it has no plan of its own", hint="its children have plans")
        t.set_section(canonical, text)

    def _write_section(self, ref: str, name: str, compose) -> Ticket:
        canonical = self._section_name(name)
        self.warnings = []

        def fn(t: Ticket) -> dict:
            text = compose(t.section(canonical))
            before = Ticket(meta=t.meta, sections=dict(t.sections))
            self._put_section(t, canonical, text)
            if canonical == "Acceptance criteria":
                unproven = evidence.ticked_without_evidence(t, before=before)
                if unproven:
                    raise ValidationError(
                        "cannot tick " + ", ".join(f"AC{n}" for n in unproven) + " without evidence",
                        hint="add a line such as `- AC1: <what proved it> (<command or link>)` to Verification first")
            self._log(t, f"updated {canonical}")
            if t.status == "backlog" and requirements_required(self.ws, t):
                from orch.core.body import empty_gate_warnings
                self.warnings = empty_gate_warnings(t)
            return {"section": canonical}

        return self._mutate(ref, "ticket.edited", fn)

    def set_sections(self, ref: str, sections: dict[str, str]) -> Ticket:
        """Replace several sections in one locked write (#216), with the rules of `set_section` for each: either all
        are written, with one Log line and one event, or none is. Evidence written in the same call counts for a
        ticked criterion. No gate is touched: a changed section only invalidates, as a single write does."""
        if not sections:
            raise UsageError("no section given")
        names: dict[str, str] = {}
        for raw, text in sections.items():
            canonical = self._section_name(raw)
            if canonical in names:
                raise UsageError(f"{canonical} is given twice")
            names[canonical] = text
        self.warnings = []

        def fn(t: Ticket) -> dict:
            before = Ticket(meta=t.meta, sections=dict(t.sections))
            for canonical, text in names.items():
                self._put_section(t, canonical, text)
            if "Acceptance criteria" in names:
                unproven = evidence.ticked_without_evidence(t, before=before)
                if unproven:
                    raise ValidationError(
                        "cannot tick " + ", ".join(f"AC{n}" for n in unproven) + " without evidence",
                        hint="add a line such as `- AC1: <what proved it> (<command or link>)` to Verification first")
            self._log(t, "updated " + ", ".join(names))
            if t.status == "backlog" and requirements_required(self.ws, t):
                from orch.core.body import empty_gate_warnings
                self.warnings = empty_gate_warnings(t)
            return {"section": ", ".join(names), "sections": list(names)}

        return self._mutate(ref, "ticket.edited", fn)

    def link(self, ref: str, *, repo: str | None = None, pr: str | None = None, branch: str | None = None,
             worktree: str | None = None, external: str | None = None, epic: str | None = None,
             no_epic: bool = False, sprint: str | None = None, no_sprint: bool = False,
             pr_state: str | None = None) -> Ticket:
        if not any((repo, pr, branch, worktree, external, epic, no_epic, sprint, no_sprint)):
            raise UsageError("nothing to link",
                             hint="pass --repo, --pr, --branch, --worktree, --external, --epic or --sprint")
        if pr_state is not None and not pr:
            raise UsageError("--state needs --pr", hint="orch link <id> --pr <number|url> --state merged")
        if pr_state is not None and pr_state not in PR_STATES:
            raise UsageError(f"unknown PR state {pr_state!r}", hint="one of: " + ", ".join(PR_STATES))
        if (epic and no_epic) or (sprint and no_sprint):
            raise UsageError("pass --epic or --no-epic (--sprint or --no-sprint), not both")
        sprint_id = self._sprint(sprint) if sprint else None
        self._bound_target(epic, "linking into an epic")
        if (branch or worktree) and not repo:
            from orch.core import prlink
            repo = prlink.repo_of(self.ws, None).name  # the only repo; several are refused with their names (#214)
        if pr:
            from orch.core import prlink
            number = prlink.pr_number(pr)
            if number is not None:  # a number resolves against the repo's origin remote (#14)
                repo, pr = prlink.resolve(self.ws, repo, number)
            elif not repo:
                repo = prlink.repo_for_url(self.ws, pr)  # the repo whose origin the URL is in; any --repo name is kept

        def fn(t: Ticket) -> dict:
            changed: dict = {}
            new_repo = bool(repo) and repo not in t.meta.setdefault("repos", [])
            if new_repo:
                t.meta["repos"].append(repo)
            if branch:
                t.meta.setdefault("branches", {})[repo] = branch
                changed["branch"] = branch
            if worktree:
                t.meta.setdefault("worktrees", {})[repo] = worktree
                changed["worktree"] = worktree
            if pr:
                prs = t.meta.setdefault("prs", [])
                known = next((p for p in prs if isinstance(p, dict) and p.get("url") == pr), None)
                if known is None:  # "unknown" until a provider or --state says otherwise (#165)
                    prs.append({"repo": repo, "url": pr, "state": pr_state or "unknown"})
                elif pr_state:
                    known["state"] = pr_state
                changed["pr"] = pr + (f" ({pr_state})" if pr_state else "")
            if external:
                ext = self._external(external)
                exts = t.meta.setdefault("external", [])
                if not any(str(x.get("key", "")).upper() == ext["key"].upper() for x in exts if isinstance(x, dict)):
                    exts.append(ext)
                changed["external"] = ext["key"]
            if epic or no_epic:
                changed["epic"] = self._reparent(t, epic)
            if sprint_id:
                t.meta["sprint"] = changed["sprint"] = sprint_id
            if no_sprint:
                t.meta.pop("sprint", None)
                changed["sprint"] = "none"
            if new_repo and not changed:  # a repo-only link (#164)
                changed["repo"] = repo
            if changed:
                self._log(t, "linked " + ", ".join(f"{k} {v}" for k, v in changed.items()))
            return changed

        return self._mutate(ref, "ticket.edited", fn)

    def set_due(self, ref: str, due: str | None) -> Ticket:
        """Set the ticket's due date (`YYYY-MM-DD`), or clear it with None (#174). Planning only, like a sprint: no
        gate binds it, so agents may change it; the Log and a `ticket.edited` event record each change."""
        from orch.core.due import checked_due
        value = checked_due(due) if due is not None else None

        def fn(t: Ticket) -> dict:
            if value is None:
                t.meta.pop("due", None)
                self._log(t, "due date cleared")
            else:
                t.meta["due"] = value
                self._log(t, f"due {value}")
            return {"due": value or "none"}

        return self._mutate(ref, "ticket.edited", fn)

    def label(self, ref: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> Ticket:
        """Add or remove labels (#173). A label already there (add) or not there (remove) is skipped; when nothing
        changes, nothing is written. Logged and recorded as a `ticket.edited` event like `link`."""
        add, remove = check_labels(add or []), check_labels(remove or [])
        if not add and not remove:
            raise UsageError("no label given", hint="e.g. `orch label add <id> customer:arbonia`")

        def changes(t: Ticket) -> tuple[list[str], list[str]]:
            have = _current_labels(t)
            return [x for x in add if x not in have], [x for x in remove if x in have]

        entry = store.resolve(self.ws, ref)
        _, current = store.load(self.ws, entry.id)
        if changes(current) == ([], []):
            return current

        def fn(t: Ticket) -> dict:
            added, removed = changes(t)
            t.meta["labels"] = [x for x in _current_labels(t) if x not in removed] + added
            parts = ([f"added {', '.join(added)}"] if added else []) + ([f"removed {', '.join(removed)}"] if removed else [])
            self._log(t, "labels " + "; ".join(parts))
            return {"labels_added": added, "labels_removed": removed}

        return self._mutate(entry.id, "ticket.edited", fn)

    def unlink_worktree(self, ref: str, repo: str) -> Ticket:
        """Drop the ticket's worktree link in `repo` (`orch worktree remove`); repo and branch links stay."""
        def fn(t: Ticket) -> dict:
            wts = t.meta.get("worktrees")
            if not isinstance(wts, dict) or repo not in wts:
                raise UsageError(f"{t.id} has no worktree in {repo}")
            path = wts.pop(repo)
            self._log(t, f"removed worktree {path}")
            return {"worktree_removed": path, "repo": repo}

        return self._mutate(ref, "ticket.edited", fn)

    def _reparent(self, t: Ticket, epic: str | None) -> str:
        """Put `t` into the epic `epic` (None: out of its epic). An approved epic's set of children is part of what
        the human approved, so only the human moves a ticket into or out of one."""
        from orch.core.epics import epic_approved, parent_epic
        old = parent_epic(self.ws, t)
        target = self._epic_target(epic, child_type=str(t.meta.get("type"))) if epic else None
        if target is not None and target.id == t.id:
            raise ValidationError("a ticket cannot be its own epic")
        if target is None and old is None:
            raise ValidationError(f"{t.id} is not in an epic")
        if old is not None and target is not None and old.id == target.id:
            return target.id
        if not self.actor.is_human:
            for side in (old, target):
                if side is not None and epic_approved(side.meta):
                    raise HumanOnlyError(f"moving a ticket into or out of the approved epic {side.id} is a human-only "
                                         "action", hint=HUMAN_HINT)
        t.meta["parent"] = target.id if target else None
        return target.id if target else "none"

    def set_extra(self, ref: str, key: str, value) -> Ticket:
        """Set a namespaced frontmatter key: `x-<addon>[...]` for addons, any `x-...` key otherwise."""
        via = self.actor.via
        if via.startswith("addon:"):
            prefix = f"x-{via.removeprefix('addon:')}"
            ok = key == prefix or (key.startswith(prefix) and key[len(prefix)] in ":-._")
        else:
            prefix = "x-"
            ok = key.startswith(prefix) and len(key) > len(prefix)
        if not ok:
            raise UsageError(f"extra key {key!r} is not in this actor's namespace", hint=f"use a key starting with {prefix}")

        def fn(t: Ticket) -> dict:
            t.meta[key] = value
            self._log(t, f"set {key}")
            return {"key": key}

        return self._mutate(ref, "ticket.edited", fn)

    # -- moves -----------------------------------------------------------------------

    def move(self, ref: str, to: str) -> Ticket:
        if to not in STATUSES:
            raise UsageError(f"unknown status {to!r}", hint="one of: " + ", ".join(STATUSES))

        self.warnings = []

        def fn(t: Ticket) -> dict:
            frm = t.status
            blockers = self._open_blockers(t) if (frm, to) == ("open", "in-progress") else ()
            if to == "testing" and not self.actor.is_human:
                from orch.core.ledger import require_signed
                require_signed(self.ws, t, ("requirements",)
                               + (("plan",) if t.meta.get("size") not in self._skip_sizes else ()))
            check_move(t, to, self.actor, plan_skip_sizes=self._skip_sizes, command="move", open_blockers=blockers,
                       requirements_skip_sizes=self._req_skip_sizes)
            if (frm, to) == ("in-progress", "testing"):
                loose = self._register_loose(t)
                self.warnings = self._handover_warnings(t)
                if loose:
                    self.warnings.insert(0, "linked files found in the ticket's artifact folder: " + ", ".join(loose)
                                         + f" (next time add them with `orch artifact add {t.id} <file> --kind …`)")
            t.meta["status"] = to
            if to == "backlog":
                clear_gate(t, "requirements")
                clear_gate(t, "plan")
            self._log(t, f"moved {frm} → {to}")
            return {}

        return self._mutate(ref, "ticket.moved", fn)

    def _handover_warnings(self, t: Ticket) -> list[str]:
        """What a move to testing allows but the human will miss: criteria without evidence (#AC) and, in a
        workspace with repos, work that names no branch or PR (#14). Warnings only; the move goes ahead."""
        out = []
        unproven = evidence.missing(t)
        if unproven:
            out.append("no evidence for " + ", ".join(f"AC{n}" for n in unproven)
                       + " in Verification (cite them as `- AC1: <what proved it>`)")
        from orch.core import artifacts as art
        cited = art.mentions(t, ("Verification",))
        if cited:
            out.append("Verification names evidence the ticket does not link: " + ", ".join(cited[:5])
                       + ("…" if len(cited) > 5 else "")
                       + f"; add each with `orch artifact add {t.id} <file>` or `--url <link>` (add `--ac <n>`)")
        visual = art.needs_proof(t)
        if visual and not art.entries(t):
            out.append("no artifacts at all, but " + ", ".join(f"AC{n}" for n in visual)
                       + " asks for something to look at; add the screenshot or report with "
                       f"`orch artifact add {t.id} <file> --ac <n> --inline`")
        if not self.actor.is_human and "```orch" not in t.section("Verification"):  # a nudge for agents, not the human
            out.append("Verification holds no widget; a `checks` widget with one row per acceptance criterion reads "
                       f"faster than prose (`orch widget add {t.id} --section Verification --type checks --data "
                       "'{\"rows\": [...]}' --source \"<command>\"`)")
        git = self.ws.config.get("git") if isinstance(self.ws.config.get("git"), dict) else {}
        has_repos = bool(git.get("repos")) or (self.ws.root / ".git").exists()
        if has_repos and not t.meta.get("prs") and not t.meta.get("branches"):
            from orch.addons.api import workspace_repos
            where = " --repo <name>" if len(workspace_repos(self.ws)) > 1 else ""
            out.append(f"no branch or PR is linked; link the work with `orch link {t.id}{where} --pr <number>` "
                       f"or `--branch <name>` so the human finds it")
        return out

    def close(self, ref: str, reason: str, resolution: str = "completed", by: str | None = None) -> Ticket:
        """Human only: any status but done → done, e.g. when the external issue was closed (spec v2 §13.2).
        `resolution` says why (RESOLUTIONS); superseded and duplicate name the replacing ticket in `by`."""
        require_human(self.actor, "closing a ticket")
        reason = " ".join((reason or "").split())
        if not reason:
            raise UsageError("closing a ticket needs a reason")
        _choice("resolution", resolution, RESOLUTIONS)
        if resolution in SUCCEEDED and not by:
            raise UsageError(f"closing as {resolution} needs the ticket that replaces it", hint="--by <ID>")
        if by and resolution not in SUCCEEDED:
            raise UsageError(f"--by is only for {' or '.join(SUCCEEDED)}")
        succ = store.resolve(self.ws, by).id if by else None

        def fn(t: Ticket) -> dict:
            _refuse_hidden("ticket title", t.title)
            if succ == t.id:
                raise UsageError(f"{t.id} cannot replace itself")
            check_move(t, "done", self.actor, plan_skip_sizes=self._skip_sizes, command="close")
            skipped = _skip_open_tasks(t, reason)
            t.meta["status"] = "done"
            t.meta["claim"] = dict(_EMPTY_CLAIM)
            t.meta["resolution"] = resolution
            t.meta.pop("superseded_by", None)
            why = {}
            if resolution != "completed":
                why = {"resolution": resolution, **({"superseded_by": succ} if succ else {})}
                if succ:
                    t.meta["superseded_by"] = succ
            self._ledger(t, "close", reason=reason, **why)
            how = "" if resolution == "completed" else f" as {resolution}" + (f" by {succ}" if succ else "")
            self._log(t, f"closed{how}: {reason}" + (f" (skipped {', '.join(skipped)})" if skipped else ""))
            return {"reason": reason, "command": "close", **why, **({"tasks_skipped": skipped} if skipped else {})}

        return self._mutate(ref, "ticket.moved", fn)

    def reopen(self, ref: str, reason: str) -> Ticket:
        """Human only: done → open when the requirements are still approved (or its size skips them), else backlog."""
        require_human(self.actor, "reopening a ticket")
        reason = " ".join((reason or "").split())
        if not reason:
            raise UsageError("reopening a ticket needs a reason")

        def fn(t: Ticket) -> dict:
            to = "open" if (gate_state(t, "requirements") == "approved" or not requirements_required(self.ws, t)) \
                else "backlog"
            check_move(t, to, self.actor, plan_skip_sizes=self._skip_sizes, command="reopen")
            t.meta["status"] = to
            t.meta.setdefault("gates", {}).pop("verify", None)
            t.meta.pop("resolution", None)
            t.meta.pop("superseded_by", None)
            self._ledger(t, "reopen", reason=reason)
            if to == "backlog":
                clear_gate(t, "requirements")
                clear_gate(t, "plan")
            self._log(t, f"reopened → {to}: {reason}")
            return {"reason": reason, "command": "reopen"}

        return self._mutate(ref, "ticket.moved", fn)

    # -- artifacts -------------------------------------------------------------------

    def artifact_add(self, ref: str, file: Path, name: str | None = None, *, context: bool = False,
                     stream=None, kind: str | None = None, label: str | None = None, task: str | None = None,
                     ac: int | None = None, inline: bool = False, replace: bool = False, _run: dict | None = None) -> Path:
        """Copy `file` into the ticket's artifacts and link it in the ticket (frontmatter `artifacts`, with its
        sha256, a kind and optionally the task or criterion it proves). With `stream` (an open, already checked file
        of `file`), the bytes come from the stream, not from the path again. A file that already lies in the ticket's
        artifact folder is linked where it is. `inline` (needs `ac`) also writes a Verification line for that
        criterion that shows the file. `replace` overwrites a linked file of the same name (its new hash then
        invalidates an approval whose text shows it)."""
        from orch.core import artifacts as art
        entry = store.resolve(self.ws, ref)
        src = Path(file)
        if stream is None and (src.is_symlink() or not src.is_file()):
            raise UsageError(f"not a file: {file}")
        base = self.ws.artifacts_dir / entry.id
        in_place = stream is None and name is None and _inside(src, base)
        opened = None
        if stream is None:
            from orch.core.fsutil import agent_source
            # an agent copies only workspace files into an artifact; a factory session's file is read from the one
            # descriptor that was checked (never from the path again)
            opened = agent_source(self.ws, self.actor, src)
            if opened is not None and not in_place:
                stream = opened
        try:
            return self._artifact_add(ref, entry, src, name, stream, base, in_place, context=context, kind=kind,
                                      label=label, task=task, ac=ac, inline=inline, replace=replace)
        finally:
            if opened is not None:
                opened.close()

    def _artifact_add(self, ref, entry, src, name, stream, base, in_place, *, context, kind, label, task, ac, inline,
                      replace) -> Path:
        from orch.core import artifacts as art
        fname = src.resolve().relative_to(base.resolve()).as_posix() if in_place else _artifact_name(name or src.name)
        if not art.safe_name(fname):
            raise UsageError(f"invalid artifact name {name or src.name!r}")
        kind = "receipt" if _run is not None else _artifact_kind(kind, art.guess_kind(fname))
        label = _artifact_label(label)
        if kind == "feedback":  # a person's own words and pictures: an agent cannot file one in a human's name
            require_human(self.actor, "attaching feedback")
        if inline and ac is None:
            raise UsageError("--inline writes a Verification line for one criterion", hint="pass --ac <n> as well")
        limit = art.max_bytes(self.ws)
        if stream is None and src.stat().st_size > limit:
            raise ValidationError(f"{src.name} is larger than the artifact limit of {limit} bytes",
                                  hint="attach a smaller excerpt, or link where the full file lives with --url")
        dest = base / fname

        def fn(t: Ticket) -> dict:
            _check_artifact_targets(t, task, ac)
            known = art.find(t, fname)
            if isinstance(known, dict) and known.get("kind") == "receipt" and _run is None:
                raise UsageError(f"{fname} is a receipt: only `orch task done --run` writes it",
                                 hint="attach your file under another name with --name")
            if not in_place and dest.exists() and not replace:
                raise ValidationError(f"artifact {entry.id}/{fname} already exists",
                                      hint="pass --name to store it under another name, or --replace")
            if not in_place and not self.dry_run:
                _copy_capped(src, stream, dest, limit)
            item = {"name": fname, "kind": kind}
            if not self.dry_run:
                item.update(sha256=art.file_sha256(dest), size=dest.stat().st_size)
            item["added"] = stamp()
            item["by"] = self.actor.to_str()
            if _run is not None:
                item["run"] = _run
            item.update(_artifact_extras(label, task, ac, context))
            _put_entry(t, item, lambda e: e.get("name") == fname)
            if inline:
                _inline_evidence(t, ac, label or f"{kind} {fname}",
                                 ("!" if art.is_image(fname) else "") + f"[{_alt(label or fname)}](artifact:{fname})")
            self._log(t, f"{'replaced' if known else 'added'} artifact {fname} ({kind})" + _for(task, ac)
                      + (" · evidence in Verification" if inline else ""))
            return {"name": fname, "kind": kind, **_artifact_extras(label, task, ac, context)}

        self._mutate(ref, "artifact.added", fn)
        return dest

    def artifact_add_many(self, ref: str, files: list[Path], *, name: str | None = None, context: bool = False,
                          kind: str | None = None, label: str | None = None, task: str | None = None,
                          ac: int | None = None, inline: bool = False, replace: bool = False) -> list[Path]:
        """Add every file in `files` as `artifact_add` does, all or nothing (#215): each file, name, size and target
        is checked first, then the files are copied and linked in one `_mutate`, so the call either fully succeeds
        or leaves the ticket and its artifact folder as they were. One Log line and one event name them all.
        `name` and `inline` take exactly one file; `replace` applies to every file."""
        from orch.core import artifacts as art
        files = [Path(f) for f in files]
        if not files:
            raise UsageError("no file given")
        if len(files) > 1 and (name or inline):
            raise UsageError("--name and --inline take exactly one file", hint="add the files one by one")
        if len(files) == 1:
            return [self.artifact_add(ref, files[0], name, context=context, kind=kind, label=label, task=task,
                                      ac=ac, inline=inline, replace=replace)]
        entry = store.resolve(self.ws, ref)
        base = self.ws.artifacts_dir / entry.id
        label = _artifact_label(label)
        limit = art.max_bytes(self.ws)
        plan: list[tuple[Path, str, str, bool]] = []  # (source, stored name, kind, already in the folder)
        for src in files:
            if src.is_symlink() or not src.is_file():
                raise UsageError(f"not a file: {src}")
            in_place = _inside(src, base)
            fname = src.resolve().relative_to(base.resolve()).as_posix() if in_place else _artifact_name(src.name)
            if not art.safe_name(fname):
                raise UsageError(f"invalid artifact name {src.name!r}")
            if any(fname == p[1] for p in plan):
                raise UsageError(f"{fname} is given twice", hint="rename one of the files; names are unique per ticket")
            k = _artifact_kind(kind, art.guess_kind(fname))
            if k == "feedback":
                require_human(self.actor, "attaching feedback")
            if src.stat().st_size > limit:
                raise ValidationError(f"{src.name} is larger than the artifact limit of {limit} bytes",
                                      hint="attach a smaller excerpt, or link where the full file lives with --url")
            plan.append((src, fname, k, in_place))

        def fn(t: Ticket) -> dict:
            _check_artifact_targets(t, task, ac)
            for _, fname, _, in_place in plan:
                known = art.find(t, fname)
                if isinstance(known, dict) and known.get("kind") == "receipt":
                    raise UsageError(f"{fname} is a receipt: only `orch task done --run` writes it",
                                     hint="attach your file under another name")
                if not in_place and (base / fname).exists() and not replace:
                    raise ValidationError(f"artifact {entry.id}/{fname} already exists",
                                          hint="rename the file, or pass --replace")
            created: list[Path] = []
            backups: list[tuple[Path, Path]] = []  # (dest, where the replaced file waits)
            items: list[dict] = []
            try:
                for src, fname, k, in_place in plan:
                    dest = base / fname
                    if not in_place and not self.dry_run:
                        if dest.exists():
                            backup = dest.with_name(f".{dest.name}.{os.getpid()}.bak")
                            os.replace(dest, backup)
                            backups.append((dest, backup))
                        else:
                            created.append(dest)
                        _copy_capped(src, None, dest, limit)
                    item = {"name": fname, "kind": k}
                    if not self.dry_run:
                        item.update(sha256=art.file_sha256(dest), size=dest.stat().st_size)
                    item["added"] = stamp()
                    item["by"] = self.actor.to_str()
                    item.update(_artifact_extras(label, task, ac, context))
                    items.append(item)
            except BaseException:
                for dest in created:
                    dest.unlink(missing_ok=True)
                for dest, backup in backups:
                    os.replace(backup, dest)
                raise
            for _, backup in backups:
                backup.unlink(missing_ok=True)
            replaced = [p[1] for p in plan if art.find(t, p[1])]
            for item in items:
                _put_entry(t, item, lambda e, n=item["name"]: e.get("name") == n)
            names = [p[1] for p in plan]
            self._log(t, f"added {len(names)} artifacts {', '.join(names)}" + _for(task, ac)
                      + (f" (replaced {', '.join(replaced)})" if replaced else ""))
            return {"name": ", ".join(names), "names": names, **_artifact_extras(label, task, ac, context)}

        self._mutate(ref, "artifact.added", fn)
        return [base / p[1] for p in plan]

    def artifact_link(self, ref: str, url: str, *, label: str | None = None, kind: str | None = None,
                      task: str | None = None, ac: int | None = None, inline: bool = False,
                      context: bool = False) -> dict:
        """Link a web page (a CI run, a dashboard, a PR check, a report somewhere else) in the ticket. Only http(s)
        URLs; orch never fetches them. The same URL again updates its label, kind and targets."""
        from orch.core import artifacts as art
        url = _artifact_url(url)
        kind = _artifact_kind(kind, "link")
        label = _artifact_label(label)
        if inline and ac is None:
            raise UsageError("--inline writes a Verification line for one criterion", hint="pass --ac <n> as well")
        item = {"url": url, "kind": kind, "added": stamp(), "by": self.actor.to_str(), **_artifact_extras(label, task, ac, context)}

        def fn(t: Ticket) -> dict:
            _check_artifact_targets(t, task, ac)
            _put_entry(t, item, lambda e: e.get("url") == url)
            if inline:
                _inline_evidence(t, ac, label or f"{kind} {url}", f"[{_alt(label or url)}]({url})")
            self._log(t, f"linked {kind} {url}" + _for(task, ac) + (" · evidence in Verification" if inline else ""))
            return {"url": url, "kind": kind, **_artifact_extras(label, task, ac, context)}

        self._mutate(ref, "artifact.added", fn)
        return item

    def artifact_scan(self, ref: str) -> list[str]:
        """Link the files that were written straight into artifacts/<ticket>/ or static/<ticket>/ without
        `orch artifact add`. Returns what was linked (static files as `static:<path>`)."""
        from orch.core import artifacts as art
        found: list[str] = []

        def fn(t: Ticket) -> dict | None:
            found.extend(self._register_loose(t))
            for path in art.unregistered_static(self.ws, t):
                _put_entry(t, {"static": path, "kind": art.guess_kind(path), "added": stamp(), "by": self.actor.to_str()},
                           lambda e, p=path: e.get("static") == p)
                found.append(f"static:{path}")
            if found:
                self._log(t, "linked " + ", ".join(found))
            return {"scanned": list(found)}

        entry = store.resolve(self.ws, ref)
        if not art.unregistered(self.ws, store.load(self.ws, entry.id)[1]) and \
                not art.unregistered_static(self.ws, store.load(self.ws, entry.id)[1]):
            return []
        self._mutate(ref, "artifact.added", fn)
        return found

    def _register_loose(self, t: Ticket) -> list[str]:
        """Link the unlinked files in artifacts/<ticket>/ (kind guessed, hash recorded); returns their names."""
        from orch.core import artifacts as art
        names = art.unregistered(self.ws, t)
        for n in names:
            p = self.ws.artifacts_dir / t.id / n
            _put_entry(t, {"name": n, "kind": art.guess_kind(n), "sha256": art.file_sha256(p),
                           "size": p.stat().st_size, "added": stamp(), "by": self.actor.to_str()}, lambda e, n=n: e.get("name") == n)
        return names

    # -- questions -------------------------------------------------------------------

    def ask(self, ref: str, raw_questions: list) -> tuple[Ticket, list[dict]]:
        added: list[dict] = []

        def fn(t: Ticket) -> dict:
            if t.status == "done":
                raise TransitionError(f"{t.id} is done; open a follow-up instead", hint=f"orch new --from {t.id} --title ...")
            if not self.actor.is_human:
                from orch.core.permits import charter_epic
                epic = charter_epic(self.ws, t)
                if epic is not None:
                    raise ValidationError(
                        f"{t.id} is part of the AI Factory epic {epic.id}: questions are not asked there",
                        hint="decide within the epic's text and record why with `orch log` (a note, not an answer), "
                             "or leave the item out and list it as not built; a missing permission goes through "
                             "`orch permit request`")
            qs = build_questions(raw_questions, t.meta.get("questions") or [], stamp())
            t.meta.setdefault("questions", []).extend(qs)
            added.extend(qs)
            moved = False
            if t.status == "in-progress" and any(q["blocking"] for q in qs):
                check_move(t, "waiting", self.actor, plan_skip_sizes=self._skip_sizes)
                t.meta["status"] = "waiting"
                moved = True
            blocking = [q["id"] for q in qs if q["blocking"]]
            blocked = block_doing_for(t, blocking[0]) if blocking else None
            self._log(t, "asked " + ", ".join(q["id"] for q in qs) + (" → waiting" if moved else "")
                      + (f" · {blocked} blocked on {blocking[0]}" if blocked else ""))
            data = {"qids": [q["id"] for q in qs]}
            if blocked:
                data["task_blocked"] = blocked
            return data

        ticket = self._mutate(ref, "question.asked", fn)
        return ticket, added

    def _check_answer(self, t: Ticket, qid: str, value: str, expected_hash: str | None):
        """Every refusal an answer can meet, nothing written: returns (question, the validated answer)."""
        q = find_question(t, qid)
        _refuse_hidden("question", t.title, q.get("text"), q.get("why"), value,
                       *(f"{o.get('key')} {o.get('label')} {o.get('cost') or ''}" for o in q.get("options") or []
                         if isinstance(o, dict)))
        if expected_hash != question_hash(q):
            raise ValidationError(f"the question changed since this answer was given ({q['id']}); nothing was applied")
        if q.get("answer") not in (None, ""):
            raise ValidationError(f"{q['id']} is already answered ({q['answer']})")
        return q, validate_answer(q, value)

    def _record_answer(self, q: dict, answer, note: str | None, t: Ticket) -> None:
        q["answer"] = answer
        q["note"] = note or None
        q["answered"] = stamp()
        q["via"] = self.actor.via
        self._ledger(t, "answer", qid=q["id"], answer=q["answer"], question_hash=question_hash(q))

    def _after_answers(self, t: Ticket) -> tuple[bool, str | None]:
        """Once the last blocking question is answered: waiting -> in-progress, and restart the tasks it blocked."""
        moved = False
        if t.status == "waiting" and not unanswered_blocking(t):
            check_move(t, "in-progress", self.actor, plan_skip_sizes=self._skip_sizes, command="auto")
            t.meta["status"] = "in-progress"
            moved = True
        return moved, (None if unanswered_blocking(t) else restart_answered(t))

    def answer(self, ref: str, qid: str, value: str, note: str | None = None, *,
               expected_hash: str | None = None, attachments: list[str] | None = None) -> Ticket:
        """`expected_hash` (a `question_hash`, required) binds the answer to the question text and options the human
        saw. `attachments`: the feedback artifacts (pasted images) stored with the answer, named in the event."""
        require_human(self.actor, "answering questions")
        _require_seen(expected_hash, "an answer")

        def fn(t: Ticket) -> dict:
            q, answer = self._check_answer(t, qid, value, expected_hash)
            self._record_answer(q, answer, note, t)
            moved, restarted = self._after_answers(t)
            shown = ", ".join(q["answer"]) if isinstance(q["answer"], list) else q["answer"]
            extra = _feedback_extra(None, attachments)
            self._log(t, f"answered {q['id']}: {shown}" + (f" — {note}" if note else "") + _feedback_note(extra)
                      + (" → in-progress" if moved else "") + (f" · {restarted} restarted" if restarted else ""))
            data = {"qid": q["id"], "answer": q["answer"], **extra}
            if note:
                data["note"] = note
            if restarted:
                data["task_restarted"] = restarted
            return data

        return self._mutate(ref, "question.answered", fn)

    def answer_many(self, ref: str, items: list[dict]) -> Ticket:
        """#219: answer several questions of one ticket after one human confirmation, under one lock. Each item is
        {"qid", "value", "expected_hash"[, "note"]}; every answer is bound to the `question_hash` of the question the
        human was shown, exactly as `answer` is, with one signed ledger entry and one `question.answered` event per
        question. All or nothing: if any question changed, is already answered or the answer is invalid, nothing is
        written."""
        require_human(self.actor, "answering questions")
        if not items:
            raise ValidationError("no answers given")
        seen: set[str] = set()
        for it in items:
            key = str(it["qid"]).strip().upper()
            if key in seen:
                raise UsageError(f"{key} is listed more than once: one answer per question")
            seen.add(key)
            _require_seen(it.get("expected_hash"), "an answer")

        def fn(t: Ticket) -> list:
            checked = [(it, *self._check_answer(t, it["qid"], it["value"], it["expected_hash"])) for it in items]
            records = []
            for it, q, answer in checked:
                self._record_answer(q, answer, it.get("note"), t)
            moved, restarted = self._after_answers(t)
            for n, (it, q, _answer) in enumerate(checked):
                shown = ", ".join(q["answer"]) if isinstance(q["answer"], list) else q["answer"]
                last = n == len(checked) - 1
                note = it.get("note")
                self._log(t, f"answered {q['id']}: {shown}" + (f" — {note}" if note else "")
                          + (" → in-progress" if moved and last else "")
                          + (f" · {restarted} restarted" if restarted and last else ""))
                data = {"qid": q["id"], "answer": q["answer"]}
                if note:
                    data["note"] = note
                if restarted and last:
                    data["task_restarted"] = restarted
                records.append(("question.answered", data))
            return records

        return self._mutate_events(ref, fn)

    # -- gates -----------------------------------------------------------------------

    def approve(self, ref: str, gate: str, *, expected_hash: str | None = None,
                despite_open_question: bool = False, delegate: dict | None = None,
                skip_if_signed: bool = False) -> Ticket:
        """`skip_if_signed` (batch approval): a gate already approved for exactly this hash is refused with
        "already approved" under the ticket lock, so two batches never sign the same plan twice.
        `despite_open_question`: the human read the gate and approves although a line looks like an open question
        for them (recorded in the event and the ledger). For an epic, `expected_hash` is the charter's hash and
        `delegate` ({max_children, max_size}, {} for the defaults) opts in to delegation (orch.core.epics)."""
        require_human(self.actor, "approving gates")
        _require_seen(expected_hash, "an approval")
        if gate not in GATE_SECTIONS:
            raise UsageError("gate must be requirements or plan")
        target = store.resolve(self.ws, ref)
        if (target.meta or {}).get("type") == "epic":
            return self._approve_epic(target.id, gate, expected_hash=expected_hash,
                                      despite=despite_open_question, delegate=delegate)
        if delegate is not None:
            raise UsageError("delegation is given when approving an epic", hint="orch approve <epic> requirements --delegate")

        def fn(t: Ticket) -> dict:
            if expected_hash != gate_hash(t, gate):
                raise ValidationError(f"the {gate} changed since you opened it — review again")
            if skip_if_signed:
                g = (t.meta.get("gates") or {}).get(gate) or {}
                if g.get("approved") and g.get("hash") == expected_hash:
                    raise ValidationError("already approved")
            from orch.core.gates import gate_meta, gate_parts
            _refuse_hidden(f"{gate} text", t.title, *(t.section(n) for n, _ in gate_parts(t, gate)),
                           *(v for _, v in gate_meta(t, gate)))
            moved_to = None
            asks = human_questions_in(t, gate)
            if asks and not despite_open_question:
                raise ValidationError(
                    f"cannot approve: the {gate} has a line that reads as an open question for you ({asks[0][:80]})",
                    hint=f"request changes so the agent asks it with `orch ask`; if it is not a question for you, "
                         f"approve with `orch approve {t.id} {gate} --despite-open-question` (or tick the box on "
                         f"the dashboard)")
            if gate == "plan" and unanswered_blocking(t):
                raise ValidationError("cannot approve: blocking questions open ("
                                      + ", ".join(str(q["id"]) for q in unanswered_blocking(t)) + ")")
            if gate == "requirements":
                in_place = t.status in REAPPROVE_IN_PLACE and gate_state(t, gate) == "invalidated"
                if t.status != "backlog" and not in_place:
                    raise TransitionError(f"requirements are approved in backlog, or re-approved in place after they "
                                          f"changed; {t.id} is {t.status} and its requirements are "
                                          f"{gate_state(t, gate)}")
                missing = [s for s in ("Requirements", "Acceptance criteria") if not t.section(s).strip()]
                if missing:
                    raise ValidationError(f"cannot approve: {', '.join(missing)} empty", hint=f"refine {t.id} first")
                open_qs = unanswered_blocking(t)
                if open_qs:
                    raise ValidationError("cannot approve: blocking questions open (" + ", ".join(q["id"] for q in open_qs) + ")")
                record_approval(self.ws, t, gate, self.actor, snapshot=not self.dry_run)
                self._sign_approval(t, gate, bool(asks))
                if not in_place:  # a re-approval in place keeps the status and the plan's approval
                    check_move(t, "open", self.actor, plan_skip_sizes=self._skip_sizes)
                    t.meta["status"] = moved_to = "open"
            else:
                if t.status not in ("in-progress", "waiting"):
                    raise TransitionError(f"plans are approved while in progress; {t.id} is {t.status}")
                if not t.section("Plan").strip():
                    raise ValidationError("cannot approve: Plan is empty")
                record_approval(self.ws, t, gate, self.actor, snapshot=not self.dry_run)
                self._sign_approval(t, gate, bool(asks))
            self._log(t, f"approved {gate}" + (" despite an open-question line" if asks else "")
                      + (f" → {moved_to}" if moved_to else ""))
            return {"gate": gate, "hash": t.meta["gates"][gate]["hash"], "hash_v": HASH_VERSION,
                    **({"despite_open_question": True} if asks else {})}

        return self._mutate(ref, "gate.approved", fn)

    def approve_together(self, ref: str, *, requirements_hash: str | None, plan_hash: str | None,
                         despite_open_question: bool = False) -> Ticket:
        """F2: approve a backlog ticket's requirements and its plan in one confirm, when the agent drafted both
        before handing over. Each gate is checked as `approve` checks it and bound to its own hash (both texts are
        shown in full before the confirm); it writes two `gate.approved` events and two signed ledger entries, as
        two approvals would. The plan stays a gate of its own: a size without a plan gate approves the requirements
        alone, and a later change to either text needs a new approval."""
        require_human(self.actor, "approving gates")
        _require_seen(requirements_hash, "an approval of the requirements")
        _require_seen(plan_hash, "an approval of the plan")
        target = store.resolve(self.ws, ref)
        if (target.meta or {}).get("type") == "epic":
            raise UsageError(f"{target.id} is an epic: it has no plan of its own",
                             hint=f"approve its charter: orch approve {target.id} requirements")
        from orch.core.gates import gate_meta, gate_parts, plan_required

        def fn(t: Ticket) -> list[tuple[str, dict]]:
            if t.status != "backlog":
                raise TransitionError(f"requirements and plan are approved together in backlog; {t.id} is {t.status}",
                                      hint=f"approve the plan on its own: orch approve {t.id} plan")
            if not plan_required(self.ws, t):
                raise ValidationError(f"{t.id} has no plan gate (size {t.meta.get('size')})",
                                      hint=f"approve the requirements: orch approve {t.id} requirements")
            seen = {"requirements": requirements_hash, "plan": plan_hash}
            for gate in GATE_SECTIONS:
                if seen[gate] != gate_hash(t, gate):
                    raise ValidationError(f"the {gate} changed since you opened it — review again")
                if gate_state(t, gate) != "pending":
                    raise ValidationError(f"the {gate} of {t.id} is {gate_state(t, gate)}, not waiting for approval",
                                          hint=f"approve each gate on its own: orch approve {t.id} <gate>")
                _refuse_hidden(f"{gate} text", t.title, *(t.section(n) for n, _ in gate_parts(t, gate)),
                               *(v for _, v in gate_meta(t, gate)))
            missing = [s for s in ("Requirements", "Acceptance criteria", "Plan") if not t.section(s).strip()]
            if missing:
                raise ValidationError(f"cannot approve: {', '.join(missing)} empty", hint=f"refine {t.id} first")
            asks = {gate: human_questions_in(t, gate) for gate in GATE_SECTIONS}
            first = next((a[0] for a in asks.values() if a), None)
            if first and not despite_open_question:
                raise ValidationError(
                    f"cannot approve: a line reads as an open question for you ({first[:80]})",
                    hint="request changes so the agent asks it with `orch ask`; if it is not a question for you, "
                         "tick the box on the dashboard")
            open_qs = unanswered_blocking(t)
            if open_qs:
                raise ValidationError("cannot approve: blocking questions open (" + ", ".join(str(q["id"]) for q in open_qs) + ")")
            records = []
            for gate in GATE_SECTIONS:
                record_approval(self.ws, t, gate, self.actor, snapshot=not self.dry_run)
                self._sign_approval(t, gate, bool(asks[gate]))
                if gate == "requirements":
                    check_move(t, "open", self.actor, plan_skip_sizes=self._skip_sizes)
                    t.meta["status"] = "open"
                records.append(("gate.approved", {"gate": gate, "hash": t.meta["gates"][gate]["hash"],
                                                  "hash_v": HASH_VERSION, "together": True,
                                                  **({"despite_open_question": True} if asks[gate] else {})}))
            self._log(t, "approved requirements and plan" + (" despite an open-question line" if first else "")
                      + " → open")
            return records

        return self._mutate_events(ref, fn)

    # -- epics -----------------------------------------------------------------------

    def _approve_epic(self, eid: str, gate: str, *, expected_hash, despite: bool, delegate) -> Ticket:
        """The charter approval: the epic's requirements and every open child's requirements and plan, in one
        signed decision. Everything is checked before anything is signed or written."""
        from orch.core import epics
        from orch.core.gates import gate_meta, gate_parts
        if gate != "requirements":
            raise UsageError(f"{eid} is an epic: it has only the requirements gate (its children have plans)",
                             hint=f"orch approve {eid} requirements")
        if isinstance(delegate, dict) and delegate.get("factory"):
            from orch.core.permits import enabled
            if not enabled(self.ws):
                raise UsageError("AI Factory is switched off in this workspace",
                                 hint="set factory.enabled to true in orchestrator/config.json (docs/factory.md)")
        if isinstance(delegate, dict) and delegate.get("dark"):
            from orch.core.permits import dark_on
            if delegate.get("factory") and not dark_on(self.ws):
                raise UsageError("Dark AI Factory is switched off in this checkout",
                                 hint="the human runs `orch factory dark on` in their own terminal (docs/factory.md)")
        delegate = epics.normalize_delegate(delegate)  # refuses `dark` without `factory`
        covered: dict = {}

        def fn(t: Ticket) -> dict:
            if t.status not in ("backlog", "open"):
                raise TransitionError(f"epics are approved in backlog or open; {t.id} is {t.status}")
            kids = epics.open_children(self.ws, t)  # read once: checked and hashed from the same objects
            ch = epics.charter(self.ws, t, delegate, tickets=kids)
            if expected_hash != ch["content_hash"]:
                raise ValidationError(f"{t.id} or its children changed since you opened it — review again")
            _refuse_hidden("epic text", t.title, *(t.section(n) for n, _ in gate_parts(t, gate)),
                           *(v for _, v in gate_meta(t, gate)))
            missing = [x for x in ("Requirements", "Acceptance criteria") if not t.section(x).strip()]
            if missing:
                raise ValidationError(f"cannot approve: {', '.join(missing)} empty", hint=f"refine {t.id} first")
            if unanswered_blocking(t):
                raise ValidationError("cannot approve: blocking questions open ("
                                      + ", ".join(str(q["id"]) for q in unanswered_blocking(t)) + ")")
            asks = list(human_questions_in(t, gate))
            for ct in kids:
                asks += self._check_child_for_charter(ct)
            if asks and not despite:
                raise ValidationError(
                    f"cannot approve: a line in {t.id} or its children reads as an open question for you ({asks[0][:80]})",
                    hint=f"request changes so the agent asks it with `orch ask`; if it is not a question for you, approve "
                         f"with `orch approve {t.id} requirements --despite-open-question`")
            previous = epics.latest_charter(self.ws, t.id)
            if t.status == "open" and previous and previous.get("charter") == ch["hash"] \
                    and gate_state(t, "requirements") == "approved":
                raise ValidationError(f"nothing changed since the last approval of {t.id}")
            record_approval(self.ws, t, gate, self.actor, snapshot=not self.dry_run)
            self._sign_approval(t, gate, bool(asks))
            self._ledger(t, "charter", charter=ch["hash"], epic_hash=ch["epic_hash"], hash_v=ch["hash_v"],
                         children=ch["children"], delegate=delegate,
                         **({"delegation": ch["hash"]} if delegate else {}))
            moved_to = None
            if t.status == "backlog":
                check_move(t, "open", self.actor, plan_skip_sizes=self._skip_sizes)
                t.meta["status"] = moved_to = "open"
            covered.update(ch=ch)
            ids = [c["id"] for c in ch["children"]]
            # the delegation's limits stay out of the file (event and ledger only)
            self._log(t, "approved the epic" + (f" with {', '.join(ids)}" if ids else "")
                      + (" despite an open-question line" if asks else "")
                      + (f" → {moved_to}" if moved_to else ""))
            return {"gate": gate, "hash": t.meta["gates"][gate]["hash"], "hash_v": HASH_VERSION,
                    "charter": ch["hash"], "children": ids, "delegate": delegate,
                    **({"despite_open_question": True} if asks else {})}

        epic = self._mutate(eid, "gate.approved", fn)
        for c in covered["ch"]["children"]:
            self._cover_child(c, epic.id, covered["ch"]["hash"])
        return epic

    def approve_plans(self, ref: str, expected: dict, *,
                      despite: tuple | list | set = ()) -> tuple[list[Ticket], list[tuple[str, str]]]:
        """#27: approve the plans of an epic's children after one confirmation. `expected` maps each child's id to
        the plan hash the human was shown (one entry per child); every child is approved exactly as
        `approve <child> plan` would be (one signed ledger entry and one event per child, bound to that child's plan
        hash), never anything not listed. `despite`: the children (ids) whose open-question line the human waived,
        one by one. A child that fails its checks (the plan changed since it was shown, it left the epic, a question
        opened) is skipped and reported, and so is one already approved for exactly that hash; the others go ahead.
        Returns (approved tickets, [(id, why skipped)])."""
        from orch.core import epics
        from orch.core.ids import normalize_ref
        require_human(self.actor, "approving gates")
        if not expected:
            raise ValidationError("no plans to approve", hint="orch approve <epic> plans lists the plans waiting")
        seen: dict[str, str] = {}
        for raw, h in expected.items():
            cid = normalize_ref(self.ws, str(raw)).upper()
            if cid in seen:
                raise UsageError(f"{cid} is listed more than once: one plan hash per child")
            _require_seen(h, "an approval of a plan")
            seen[cid] = h
        waived = {normalize_ref(self.ws, str(x)).upper() for x in despite}
        epic = store.resolve(self.ws, ref)
        if not epics.is_epic(epic.meta or {}):
            raise UsageError(f"{epic.id} is not an epic", hint=f"orch approve {epic.id} plan")
        kids = {e.id: e for e in epics.children(self.ws, epic.id)}
        approved, skipped = [], []
        for cid, h in sorted(seen.items()):
            if cid not in kids:
                skipped.append((cid, f"not a child of {epic.id}"))
                continue
            try:
                approved.append(self.approve(cid, "plan", expected_hash=h, despite_open_question=cid in waived,
                                             skip_if_signed=True))  # the "already approved" check is under the lock
            except HumanOnlyError:
                raise
            except OrchError as e:  # this child's checks failed (or its file is busy): the others go ahead
                skipped.append((cid, e.message))
        return approved, skipped

    def _check_child_for_charter(self, ct: Ticket) -> list[str]:
        """Refuse a child the charter cannot cover (orch.core.epics.charter_blocker); return its lines that read as
        open questions for the human."""
        from orch.core.epics import charter_blocker, is_epic
        why = charter_blocker(ct)
        if why:
            raise ValidationError(f"cannot approve the epic: {why}",
                                  hint=f"orch link {ct.id} --no-epic" if is_epic(ct)
                                  else f"request changes on {ct.id} (refine it first), or take it out with "
                                       f"`orch link {ct.id} --no-epic`")
        return human_questions_in(ct, "requirements") + (human_questions_in(ct, "plan") if ct.section("Plan").strip() else [])

    def _cover_child(self, c: dict, eid: str, charter_hash: str) -> None:
        """Write the epic approval into one covered child: its gates (with `epic`), backlog → open, one
        `gate.approved` event per gate that was newly recorded. A gate already approved for that hash is not
        stamped again (no event, no log line), and a child in testing or done is left as it is. A child that
        changed since the charter was computed is left as it is (it is then simply not covered)."""
        def fn(t: Ticket) -> list:
            if t.status in ("testing", "done"):
                return []
            records = []
            for gate in ("requirements", "plan"):
                h = c.get(gate)
                if not h or gate_hash(t, gate) != h:
                    continue
                g = (t.meta.get("gates") or {}).get(gate) or {}
                if g.get("approved") and g.get("hash") == h:
                    continue
                record_approval(self.ws, t, gate, self.actor, snapshot=not self.dry_run, epic=eid)
                records.append(("gate.approved", {"gate": gate, "hash": h, "hash_v": HASH_VERSION, "epic": eid,
                                                  "charter": charter_hash}))
            if records and t.status == "backlog" and gate_state(t, "requirements") == "approved":
                check_move(t, "open", self.actor, plan_skip_sizes=self._skip_sizes)
                t.meta["status"] = "open"
            if records:
                self._log(t, f"approved with epic {eid}: " + " and ".join(r[1]["gate"] for r in records))
            return records

        self._mutate_events(c["id"], fn)

    def epic_auto_approve(self, ref: str) -> Ticket:
        """An agent approves a child it wrote, under the epic's signed delegation (requirements and plan, once per
        child, within the limits). Recorded as `gate.delegated` events by the agent, never as a human decision."""
        from orch.core import epics, ledger
        from orch.core.events import read_events
        from orch.core.gates import plan_required
        if self.actor.is_human:
            raise UsageError("auto-approval is an agent's step under delegation; you approve with `orch approve`")

        def fn(t: Ticket) -> list:
            epic = epics.parent_epic(self.ws, t)
            if epic is None:
                raise ValidationError(f"{t.id} is not a child of an epic")
            d = epics.delegation(self.ws, epic)
            if d is None:
                return approve(t, epic)  # refuses
            # the count and the marker under one lock shared by every checkout of this config dir; approve re-checks
            with epics.delegation_lock(d["id"]):
                return approve(t, epic)

        def approve(t: Ticket, epic) -> list:
            d = epics.delegation(self.ws, epic)
            if d is None:
                raise ValidationError(f"{epic.id} has no delegation: the human approves {t.id}",
                                      hint=f"stop and wait for the human (`orch wait {t.id}`)")
            if d["paused"]:
                raise ValidationError(f"the delegation on {epic.id} is paused: the human approves {t.id}")
            if d["epic_changed"]:
                raise ValidationError(f"the requirements of epic {epic.id} changed since the human delegated: the "
                                      "delegation waits until they approve the epic again")
            if d.get("expired"):
                raise ValidationError(f"the time budget of {d['max_hours']} hours on {epic.id} is used up: the human "
                                      f"approves {t.id} or the epic again", hint=f"stop and wait (`orch wait {t.id}`)")
            why = epics.within_limits(t, d)
            if why:
                raise ValidationError(f"{t.id}: {why}")
            if t.status != "backlog":
                raise TransitionError(f"auto-approval is for new children in backlog; {t.id} is {t.status}")
            if epics.ever_chartered(self.ws, epic.id, t.id, ledger.entries(self.ws)):
                raise ValidationError(f"{t.id} was part of the human's approval of {epic.id}; changes to it need the human")
            events = read_events(self.ws)
            if not epics.created_by_agent(events, t.id):
                raise ValidationError(f"{t.id} was created by the human: the human approves it",
                                      hint="delegation covers children an agent adds")
            order = epics.delegated_children(events, d["id"])
            if t.id in order or any(isinstance(g, dict) and g.get("delegation") == d["id"]
                                    for g in (t.meta.get("gates") or {}).values()):
                raise ValidationError(f"{t.id} was auto-approved once already; the human approves changes to it")
            if epics.delegated_count(self.ws, epic.id, d["id"], events) >= d["max_children"]:
                raise ValidationError(f"the delegation's limit of {d['max_children']} auto-approved children is "
                                      f"reached: the human approves {t.id}")
            missing = [x for x in ("Requirements", "Acceptance criteria") if not t.section(x).strip()]
            needs_plan = plan_required(self.ws, t)
            if needs_plan and not t.section("Plan").strip():
                missing.append("Plan")
            if missing:
                raise ValidationError(f"cannot auto-approve: {', '.join(missing)} empty",
                                      hint="auto-approval covers requirements and plan in one step: write them first")
            if unanswered_blocking(t):
                raise ValidationError("cannot auto-approve: blocking questions open")
            if epics.hidden_in(t):
                raise ValidationError(f"cannot auto-approve: the text of {t.id} holds hidden or control characters")
            gates = ["requirements"] + (["plan"] if t.section("Plan").strip() else [])
            asks = [a for g in gates for a in human_questions_in(t, g)]
            if asks:
                raise ValidationError(f"cannot auto-approve: a line reads as an open question for the human ({asks[0][:80]})",
                                      hint="ask it with `orch ask`; the human then decides")
            records = []
            for gate in gates:
                record_approval(self.ws, t, gate, self.actor, snapshot=not self.dry_run, epic=epic.id,
                                delegation=d["id"])
                records.append(("gate.delegated", {"gate": gate, "hash": t.meta["gates"][gate]["hash"],
                                                   "hash_v": HASH_VERSION, "epic": epic.id, "delegation": d["id"]}))
            if not self.dry_run:
                epics.mark_delegated(d["id"], t.id)  # the count behind the factory's child budget
            t.meta["status"] = "open"  # backlog → open on the delegated approval (the human's move otherwise)
            self._log(t, f"auto-approved {' and '.join(gates)} under the delegation of epic {epic.id}")
            return records

        return self._mutate_events(ref, fn)

    def epic_pause(self, ref: str) -> Ticket:
        """Human only: stop further auto-approvals under the epic's delegation now (signed; the valid ones are kept)."""
        from orch.core import epics
        require_human(self.actor, "pausing a delegation")

        def fn(t: Ticket) -> dict:
            if not epics.is_epic(t):
                raise UsageError(f"{t.id} is not an epic")
            d = epics.delegation(self.ws, t)
            if d is None:
                raise ValidationError(f"{t.id} has no delegation")
            if d["paused"]:
                raise ValidationError(f"the delegation on {t.id} is paused already")
            # The pause stops further auto-approvals only: the children whose auto-approval is valid now are kept,
            # with the hashes it covers, in the signed entry (a later change to one of them still needs the human).
            kept = epics.delegated_snapshot(self.ws, t, d)
            self._ledger(t, "pause", delegation=d["id"], kept=kept)
            self._log(t, "paused the delegation")
            return {"delegation": d["id"], "kept": [k["id"] for k in kept]}

        return self._mutate(ref, "delegation.paused", fn)

    def set_widgets_html(self, on: bool) -> None:
        """`widgets.html`: whether agent-written HTML runs in ticket widgets. Turning it on is a human decision, signed
        into the ledger for this workspace and value (orch.core.ledger.widgets_html_state reads it back). Turning it
        off takes power away, so anyone may, and every off is signed as well: the newest signed entry decides, so an
        off by anyone outranks an earlier on and only a fresh human on turns it back on."""
        import json
        from orch.config.load import CONFIG_NAME
        from orch.core import ledger
        from orch.core.fsutil import atomic_write_text
        if on:
            require_human(self.actor, "turning on agent HTML in widgets")
        if self.dry_run:
            return
        path = self.ws.home / CONFIG_NAME
        with lock(self.ws, "config"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            from orch.actor import process_evidence
            ledger.record_setting(self.ws, ledger.WIDGETS_HTML, on, self.actor, process_evidence())
            widgets = raw.get("widgets") if isinstance(raw.get("widgets"), dict) else {}
            raw["widgets"] = {**widgets, "html": on}
            atomic_write_text(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
        self.ws.config.setdefault("widgets", {})["html"] = on
        self._emit(None, "setting.changed", {"setting": ledger.WIDGETS_HTML, "value": on})

    def sign_checks(self) -> dict:
        """Sign the workspace's named checks (`checks` in config.json) as they stand: what each check runs, as a digest,
        in the ledger. Human only, like `widgets.html` on. The config stays the source; a check edited afterwards
        counts as changed (orch.core.ledger.check_state) until the human signs again. Returns {name: digest}."""
        from orch.actor import process_evidence
        from orch.core import ledger
        require_human(self.actor, "signing the workspace's checks")
        digests = ledger.checks_digests(self.ws.config)
        if self.dry_run:
            return digests
        with lock(self.ws, "config"):
            ledger.record_setting(self.ws, ledger.CHECKS, digests, self.actor, process_evidence())
        self._emit(None, "setting.changed", {"setting": ledger.CHECKS, "value": sorted(digests)})
        return digests

    def set_factory_dark(self, on: bool) -> None:
        """Dark AI Factory's switch for this checkout, a signed setting (orch.core.permits.dark_on), never a config
        value. On is the human's decision; off takes power away, so anyone may sign it, and the newest entry decides."""
        from orch.actor import process_evidence
        from orch.core import ledger
        from orch.core.permits import DARK_SETTING
        if on:
            require_human(self.actor, "turning on Dark AI Factory")
        if self.dry_run:
            return
        with lock(self.ws, "config"):
            ledger.record_setting(self.ws, DARK_SETTING, on, self.actor, process_evidence())
        self._emit(None, "setting.changed", {"setting": DARK_SETTING, "value": on})

    def _epic_verdict(self, eid: str, verdict: str, message: str | None, expected_hash: str | None = None) -> Ticket:
        """One verdict for the epic: every open child must be in testing; each gets its own signed done verdict,
        then the epic is done."""
        from orch.core import epics
        if verdict != "done":
            raise UsageError("an epic's verdict is done; send a child back with `orch verdict <child> follow-up -m …`")
        epic_entry = store.resolve(self.ws, eid)
        if epic_entry.status != "open":
            raise TransitionError(f"{eid} is {epic_entry.status}; an epic's verdict is given while it is open")
        kids = [e for e in epics.children(self.ws, eid) if e.status != "done"]
        waiting = [e.id for e in kids if e.status != "testing"]
        if not kids or waiting:
            raise ValidationError("the epic verdict is given when all its open children are in testing"
                                  + (f" (not yet: {', '.join(waiting)})" if waiting else " (it has none)"))
        tickets = [store.load(self.ws, e.id)[1] for e in kids]
        if expected_hash != epics.verdict_hash(tickets, self.ws):
            raise ValidationError(f"the children of {eid} or their evidence changed since you read them — review again")
        # every per-child check before any child is closed: no partial close
        for ct in tickets:
            _refuse_hidden(f"Verification of {ct.id}", ct.title, ct.section("Verification"))
            check_move(ct, "done", self.actor, plan_skip_sizes=self._skip_sizes, command="verdict")
            _refuse_changed_gates(ct)
        note = f"accepted with epic {eid}" + (f": {message}" if message else "")
        # each child's own verdict is bound to its part of what the epic hash covered (the same objects)
        seen = {ct.id: epics.verdict_hash([ct], self.ws) for ct in tickets}
        if self.dry_run:
            for ct in tickets:
                self.verdict(ct.id, "done", note, expected_hash=seen[ct.id])
        else:
            self._close_children([e.id for e in kids], seen, note)

        def fn(t: Ticket) -> dict:
            _refuse_hidden("epic title", t.title)
            require_human(self.actor, "giving verdicts")
            t.meta["status"] = "done"
            t.meta.setdefault("gates", {})["verify"] = {"verdict": "done", "at": stamp(), "via": self.actor.via}
            self._ledger(t, "verdict", verdict="done", verify_at=t.meta["gates"]["verify"]["at"],
                         children=[e.id for e in kids])
            self._log(t, "verdict done for the epic and " + ", ".join(e.id for e in kids)
                      + (f": {message}" if message else ""))
            return {"verdict": "done", "message": message, "children": [e.id for e in kids]}

        return self._mutate(eid, "verdict.given", fn)

    def ledger_adopt(self, item: dict, typed: str) -> None:
        """Sign one decision this machine's ledger lacks (`orch ledger adopt`), after the human reviewed it and typed
        its id. Refused unless the ticket still holds exactly that decision; a gate that reads as asking the human
        an open question is not adopted (the human requests changes or approves it again instead)."""
        from orch.core import ledger
        require_human(self.actor, "adopting decisions into the ledger")
        if typed.strip().lower() != item["id"]:
            raise ValidationError(f"{item['ticket']}: the typed id does not match {item['id']}; nothing was signed")

        def fn(t: Ticket) -> dict:
            from orch.textsafe import decodes_to_hidden
            current = {i["id"]: i for i in ledger.unsigned_items(self.ws, [t])}
            it = current.get(item["id"])
            if it is None or any(it.get(k) != item.get(k) for k in ledger.SIGNED_FIELDS):
                raise ValidationError(f"{t.id} changed since it was shown, or it is signed already; nothing was signed")
            if decodes_to_hidden(it.get("text")) or decodes_to_hidden(t.title):
                raise ValidationError(f"{t.id}: the text holds hidden or control characters; request changes instead "
                                      "of adopting it")
            fields = {k: it.get(k) for k in ("gate", "hash", "hash_v", "qid", "answer", "question_hash", "verdict",
                                              "verify_at", "verdict_hash") if it.get(k) is not None}
            if it["kind"] == "gate":
                if it["hash_v"] is None:
                    raise ValidationError(f"the {it['gate']} of {t.id} changed since it was approved; it needs a new "
                                          "approval, not adoption")
                asks = human_questions_in(t, it["gate"])
                if asks:
                    raise ValidationError(f"the {it['gate']} of {t.id} has a line that reads as an open question for "
                                          f"you ({asks[0][:80]}); request changes instead of adopting it")
            self._ledger(t, it["kind"], adopted=True, **fields)
            self._log(t, f"adopted the unsigned {it['kind']} {fields.get('gate') or fields.get('qid') or ''}".rstrip()
                      + " into the ledger")
            return {"adopted": it["kind"], **{k: v for k, v in fields.items() if k in ("gate", "hash", "qid")}}

        self._mutate(item["ticket"], "ledger.adopted", fn)

    def ledger_repair(self, typed: str) -> dict:
        """Accept the one trailing entry a crash left past the ledger's head record (`orch ledger repair`): human only."""
        from orch.core import ledger
        require_human(self.actor, "repairing the ledger")
        return ledger.repair_tail(typed)

    def _sign_approval(self, t: Ticket, gate: str, despite: bool) -> None:
        g = t.meta["gates"][gate]
        self._ledger(t, "gate", gate=gate, hash=g["hash"], hash_v=g.get("hash_v"),
                     **({"despite_open_question": True} if despite else {}))

    def request_changes(self, ref: str, gate: str, message: str, *, expected_hash: str | None = None,
                        attachments: list[str] | None = None) -> Ticket:
        require_human(self.actor, "requesting changes")
        if gate not in GATE_SECTIONS:
            raise UsageError("gate must be requirements or plan")
        message = (message or "").strip()
        if not message:
            raise UsageError("say what should change")

        def fn(t: Ticket) -> dict:
            at_hash = gate_hash(t, gate)  # the gate's text the request is about
            if expected_hash is not None and expected_hash != at_hash:
                raise ValidationError(f"the {gate} changed since it was reviewed; nothing was sent")
            t.meta.setdefault("gates", {}).setdefault(gate, {})["changes_requested"] = {
                "at": stamp(), "by": self.actor.label, "message": message, "hash": at_hash,
            }
            extra = _feedback_extra(None, attachments)
            self._log(t, f"asked for changes on the {gate}: {message}" + _feedback_note(extra))
            return {"gate": gate, "message": message, "hash": at_hash, **extra}

        return self._mutate(ref, "gate.changes_requested", fn)

    def _close_children(self, ids: list[str], seen: dict, note: str) -> None:
        """Close every child of an epic verdict, or none: all their locks are held while each is re-read and checked
        against the hash it was shown with, and only then are they written."""
        from contextlib import ExitStack

        from orch.core.epics import verdict_hash
        with ExitStack() as stack:
            for tid in sorted(ids):
                stack.enter_context(lock(self.ws, tid))
            loaded = [store.load(self.ws, tid) for tid in ids]
            for _, t in loaded:  # every check before any write: no partial close
                if verdict_hash([t], self.ws) != seen[t.id]:
                    raise ValidationError(f"the criteria or evidence of {t.id} changed since you read them; no child "
                                          "was closed — review again")
                _refuse_hidden(f"Verification of {t.id}", t.title, t.section("Verification"))
                check_move(t, "done", self.actor, plan_skip_sizes=self._skip_sizes, command="verdict")
                _refuse_changed_gates(t)
            written = []
            for path, t in loaded:
                before = t.status
                data = self._verdict_fn("done", note, seen[t.id])(t) or {}
                if t.status != before:
                    data["from"], data["to"] = before, t.status
                store.save(self.ws, t, path)
                written.append((t.id, data))
        for tid, data in written:
            self._emit(tid, "verdict.given", data)

    def verdict(self, ref: str, verdict: str, message: str | None = None, *,
                expected_hash: str | None = None, acs: list[int] | None = None,
                attachments: list[str] | None = None) -> Ticket:
        """`expected_hash`: the hash of the criteria and evidence the human read (orch.core.epics.verdict_hash: for
        an epic over its open children, else over this ticket); refused when it no longer matches. A follow-up may name
        the criteria it is about (`acs`, 1-based) and the feedback artifacts stored for it (`attachments`, names under
        the ticket's artifacts): both go into the event and the Log line, so the agent reads them from `orch wait`."""
        require_human(self.actor, "giving verdicts")
        _require_seen(expected_hash, "a verdict")
        if verdict not in ("done", "follow-up"):
            raise UsageError("verdict must be done or follow-up")
        if verdict == "follow-up" and not (message or "").strip():
            raise UsageError("a follow-up verdict needs a message (-m) saying what is missing")
        target = store.resolve(self.ws, ref)
        if (target.meta or {}).get("type") == "epic":
            return self._epic_verdict(target.id, verdict, message, expected_hash)

        extra = _feedback_extra(acs, attachments) if verdict == "follow-up" else {}
        return self._mutate(ref, "verdict.given", self._verdict_fn(verdict, message, expected_hash, extra))

    def _verdict_fn(self, verdict: str, message: str | None, expected_hash: str, extra: dict | None = None):
        """The change one verdict makes to its ticket (inside the ticket's lock), checked against `expected_hash`."""
        def fn(t: Ticket) -> dict:
            from orch.core.epics import verdict_hash
            seen = verdict_hash([t], self.ws)  # what the verdict accepts, before the status changes
            if expected_hash != seen:
                raise ValidationError(f"the criteria or evidence of {t.id} changed since you read them — review again")
            if verdict == "done":
                _refuse_hidden("ticket's Verification", t.title, t.section("Verification"))
                _refuse_changed_gates(t)
            to = "done" if verdict == "done" else "in-progress"
            check_move(t, to, self.actor, plan_skip_sizes=self._skip_sizes, command="verdict")
            t.meta["status"] = to
            t.meta.setdefault("gates", {})["verify"] = {"verdict": verdict, "at": stamp(), "via": self.actor.via,
                                                           "hash": seen}
            self._ledger(t, "verdict", verdict=verdict, verify_at=t.meta["gates"]["verify"]["at"],
                         verdict_hash=seen)
            if to == "done":
                t.meta["claim"] = dict(_EMPTY_CLAIM)
                t.meta["resolution"] = "completed"
                t.meta.pop("superseded_by", None)
            self._log(t, f"verdict {verdict}" + (f": {message}" if message else "") + _feedback_note(extra))
            return {"verdict": verdict, "message": message, **(extra or {})}

        return fn

    # -- raw edits (dashboard) -------------------------------------------------------

    def replace_raw(self, ref: str, text: str, expected_mtime_ns: int | None = None) -> Ticket:
        """Replace a ticket file with human-edited text; protected fields must stay as they are."""
        require_human(self.actor, "editing a ticket file")
        entry = store.resolve(self.ws, ref)
        with lock(self.ws, entry.id):
            path, current = store.load(self.ws, entry.id)
            _record_owed_invalidations(self.ws, current)
            if expected_mtime_ns is not None and path.stat().st_mtime_ns != expected_mtime_ns:
                raise ValidationError(f"{current.id} changed since you opened it", hint="reload, then apply your edit again")
            new = parse_ticket(text, path.name)
            if new.id != current.id:
                raise ValidationError("the ticket id cannot be changed")
            changed = protected_changes(current.meta, new.meta)
            if changed:
                raise ValidationError(f"edit {', '.join(changed)} with the dashboard buttons, not in the raw file")
            check_raw_tasks(current, new)
            new.append_log(log_line(self.actor, "edited the ticket file"))
            store.save(self.ws, new, path)
        self._emit(new.id, "ticket.edited", {"raw": True})
        return new


# -- artifact helpers --------------------------------------------------------------------------------------------------

def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return base.is_dir()
    except (OSError, ValueError):
        return False


def _artifact_name(raw: str) -> str:
    """A stored file name: no folders, no characters that are unsafe on some file system, no hidden characters,
    no leading dots (hidden files) and no whitespace (so Markdown can reference it as `artifact:<name>`)."""
    from orch.textsafe import strip_hidden
    name = _UNSAFE_NAME.sub("-", strip_hidden(str(raw), keep_whitespace=False)).split("/")[-1].strip()
    return re.sub(r"\s+", "-", name).lstrip(".")


def _feedback_extra(acs: list[int] | None, attachments: list[str] | None) -> dict:
    """What a human's feedback names besides its text: the criteria (positive integers, once each, in order) and the
    feedback artifacts stored with it. Only what is given appears, so an old reader sees the old event."""
    out: dict = {}
    nums = sorted({int(n) for n in acs or [] if isinstance(n, int) and not isinstance(n, bool) and n > 0})
    if nums:
        out["acs"] = nums
    names = [str(n) for n in attachments or [] if str(n).strip()]
    if names:
        out["attachments"] = names
    return out


def _feedback_note(extra: dict | None) -> str:
    """The Log line's tail for `_feedback_extra`: " (AC1, AC5)" and " · 2 images: a.png, b.png"."""
    extra = extra or {}
    note = f" ({', '.join(f'AC{n}' for n in extra['acs'])})" if extra.get("acs") else ""
    if extra.get("attachments"):
        names = extra["attachments"]
        note += f" · {len(names)} image{'s' if len(names) != 1 else ''}: {', '.join(names)}"
    return note


def _artifact_kind(kind: str | None, default: str) -> str:
    from orch.core.artifacts import KINDS
    if kind in (None, ""):
        return default
    from orch.core.artifacts import RESERVED_KINDS
    if kind in RESERVED_KINDS:
        raise UsageError(f"the {kind} kind is written by `orch task done --run`, which runs the check itself",
                         hint="attach your own output as kind log")
    if kind not in KINDS:
        raise UsageError(f"unknown artifact kind {kind!r}",
                         hint="one of: " + ", ".join(k for k in KINDS if k not in RESERVED_KINDS))
    return kind


def _artifact_label(label: str | None) -> str | None:
    from orch.core.artifacts import MAX_LABEL
    from orch.textsafe import decodes_to_hidden
    if label is None or not str(label).strip():
        return None
    label = str(label).strip()
    if "\n" in label or "\r" in label or decodes_to_hidden(label):
        raise ValidationError("an artifact label is one line of plain text, without hidden or control characters")
    if len(label) > MAX_LABEL:
        raise ValidationError(f"an artifact label has at most {MAX_LABEL} characters")
    return label


def _artifact_url(url: str) -> str:
    from urllib.parse import urlsplit
    from orch.core.artifacts import MAX_URL
    from orch.textsafe import decodes_to_hidden
    url = str(url or "").strip()
    if decodes_to_hidden(url):
        raise ValidationError("the URL holds hidden or control characters")
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if (parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname
            or any(c.isspace() for c in url) or len(url) > MAX_URL):
        raise UsageError(f"not a web link: {url!r}", hint="only http:// and https:// URLs can be linked")
    return url


def _artifact_extras(label, task, ac, context) -> dict:
    return {**({"label": label} if label else {}), **({"task": task} if task else {}),
            **({"ac": ac} if ac is not None else {}), **({"context": True} if context else {})}


def _for(task, ac) -> str:
    return "".join([f" for {task}" if task else "", f" for AC{ac}" if ac is not None else ""])


def _check_artifact_targets(t: Ticket, task: str | None, ac: int | None) -> None:
    if ac is not None:
        total = len(evidence.criteria(t))
        if isinstance(ac, bool) or not isinstance(ac, int) or not 1 <= ac <= total:
            raise ValidationError(f"{t.id} has no criterion AC{ac}" + (f" (AC1–AC{total})" if total else ""))
    if task:
        from orch.core import tasks as tk
        try:
            ids = {x.id for x in tk.ticket_tasks(t)}
        except tk.TaskParseError:
            ids = set()
        if task not in ids:
            raise ValidationError(f"{t.id} has no task {task}", hint=f"orch task list {t.id}")


def _put_entry(t: Ticket, item: dict, same) -> None:
    items = t.meta.get("artifacts") if isinstance(t.meta.get("artifacts"), list) else []
    for i, e in enumerate(items):
        if isinstance(e, dict) and same(e):
            items[i] = item
            break
    else:
        items.append(item)
    t.meta["artifacts"] = items


def _alt(text: str) -> str:
    return re.sub(r"([\[\]\\])", r"\\\1", text)


def _inline_evidence(t: Ticket, ac: int, text: str, markdown: str) -> None:
    current = t.section("Verification").rstrip()
    block = f"- AC{ac}: {text}\n  {markdown}"
    new = f"{current}\n{block}" if current else block
    _check_section_text("Verification", new)
    t.set_section("Verification", new)


def _copy_capped(src: Path, stream, dest: Path, limit: int) -> None:
    """Write the bytes (from `stream`, else from `src`) to `dest` atomically; refuse more than `limit` bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if stream is not None:
            with tmp.open("wb") as out:
                left = limit
                for chunk in iter(lambda: stream.read(1 << 16), b""):
                    left -= len(chunk)
                    if left < 0:
                        raise ValidationError(f"the file is larger than the artifact limit of {limit} bytes")
                    out.write(chunk)
        else:
            shutil.copy2(src, tmp)
            if tmp.stat().st_size > limit:
                raise ValidationError(f"the file is larger than the artifact limit of {limit} bytes")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _record_owed_invalidations(ws, ticket) -> None:
    from orch.core.check import record_ticket_invalidations  # check imports ops, so not at module level
    record_ticket_invalidations(ws, ticket)
