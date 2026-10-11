"""``orch import v1`` (C10): plan, show, sign once, append.

The importer (:mod:`orch.importer`) maps v1 files to steps; this module is the part that touches the store and the
person's key. The order is the point:

1. **Read** the v1 workspace (read-only) and the target workspace (every ticket replayed and verified).
2. **Plan** every ticket against the real state with ``orch.model.preview``, one step after the other, each judged on
   top of the ones before it (a parent or a blocker is created before the ticket that names it). A ticket whose
   steps the model refuses is skipped as a whole, with the reason: all or nothing per ticket, decided before anything
   is signed. ``--dry-run`` stops here.
3. **Show** the person what will be signed and make them type ``IMPORT <n>`` on the terminal (``/dev/tty`` only).
4. **Sign** with one passphrase for the whole batch (the plan's digest is in the prompt; ticket-format section 14), and
   append step by step. Each step is first checked against what the ticket already holds, so an interrupted import
   resumes where it stopped and a finished one is left alone (the last step of a ticket is a ``log.added`` that says
   so; nothing is ever updated or overwritten once it is there).
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

from orch import canon, crypto
from orch.importer import TicketPlan, TicketPlanner, V1Ticket, V1Workspace, find_v1, read_v1, source_id
from orch.importer.plan import Step, clean_text, complete_text
from orch.ops import human
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import KEY_ID, Human
from orch.ops.runtime import path_hash
from orch.schema import SECTIONS_BY_TYPE
from orch.store.render import thaw

__all__ = ["run_import"]

_SHOWN = 12  # tickets listed in the review and the report before "+N more"


@dataclass
class Work:
    plan: TicketPlan
    todo: list[Step]  # what is still to do (all of it for a new ticket)
    existing: bool = False


@dataclass
class Plan:
    work: list[Work] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def events(self) -> int:
        return sum(len(w.todo) for w in self.work)

    def digest(self) -> str:
        import hashlib
        import json

        doc = [(w.plan.digest(), [s.kind for s in w.todo]) for w in self.work]
        return "sha256:" + hashlib.sha256(json.dumps(doc, separators=(",", ":")).encode()).hexdigest()


def _order(ws: V1Workspace) -> list[V1Ticket]:
    """Tickets so that a parent, a blocker and a replaced-by ticket come before the ticket that names it (a cycle is
    broken where it is found: the later edge is dropped when the ticket is planned)."""
    by_key = {t.key: t for t in ws.tickets}
    out: list[V1Ticket] = []
    state: dict[str, int] = {}

    def deps(t: V1Ticket) -> list[str]:
        m = t.meta
        names = [m.get("parent"), m.get("superseded_by"), *(m.get("blocked_by") or [])]
        return [d for d in names if isinstance(d, str) and d in by_key and d != t.key]

    for root in ws.tickets:
        stack = [(root.key, iter(sorted(deps(root))))]
        if state.get(root.key):
            continue
        state[root.key] = 1
        while stack:
            key, it = stack[-1]
            nxt = next((d for d in it if not state.get(d)), None)
            if nxt is None:
                state[key] = 2
                out.append(by_key[key])
                stack.pop()
            else:
                state[nxt] = 1
                stack.append((nxt, iter(sorted(deps(by_key[nxt])))))
    return out


class _AcceptSignatures:
    """Judges everything but signatures (the plan is judged before anybody signs; the store verifies for real)."""

    def verify_person(self, event: Any, context: Any) -> bool:
        return True

    def verify_host(self, event: Any, **kw: Any) -> bool:
        return True

    def verify_embedded(self, event: Any, **kw: Any) -> bool:
        return True


class Importer:
    def __init__(self, h: Human, v1: V1Workspace) -> None:
        self.h = h
        self.v1 = v1
        self.store = h.store
        self.person, self.device = h.who()
        self.source = source_id(v1)

    # -- planning
    def plan(self) -> Plan:
        from orch.model import Refusal, preview

        store, v1 = self.store, self.v1
        out = Plan()
        prefix = store.state.workspace.prefix
        if v1.prefix != prefix:
            raise OrchError(
                "invalid.input",
                f"the v1 prefix is {v1.prefix[:16]!r}, this workspace's is {prefix!r}",
                hint="keys are kept, so the prefixes must match: orch init --prefix " + v1.prefix[:16],
            )
        for p in v1.problems:
            out.skipped.append((p.where, p.why))
        store.load_all()
        state = store.state
        state = dataclasses.replace(
            state, _ctx=dataclasses.replace(state._ctx, verifier=_AcceptSignatures(), admit=True)
        )
        repos = frozenset(store.state.workspace.repos)
        stamp = store.peek_stamp("workspace")
        clock = _epoch(stamp["at"])
        n = 0
        known: set[str] = set()
        for t in _order(v1):
            planner = TicketPlanner(
                self.source,
                v1.home,
                self.person,
                repos,
                SECTIONS_BY_TYPE,
                frozenset(known),
            )
            try:
                tp = planner.plan(t)
            except Exception as e:  # PlanRefused, or a file that went away
                out.skipped.append((t.key, f"{e}"[:200]))
                continue
            uid = tp.uid
            other = store.uid_of(t.key)
            if other is not None and other != uid:
                out.skipped.append((t.key, "the key is taken by another v2 ticket"))
                continue
            view = store.state.tickets.get(uid)
            todo = list(tp.steps)
            if view is not None:
                mark = next((a for a in view.artifacts if a.name == "v1-import.json"), None)
                if mark is not None and mark.digest != tp.marker_digest:
                    out.already.append(f"{t.key} (v1 changed since it was imported; not touched)")
                    known.add(t.key)
                    continue
                if self._complete(uid, tp):
                    out.already.append(t.key)
                    known.add(t.key)
                    continue
                todo = [s for s in tp.steps if not _satisfied(s, view)]
            # judge the steps on top of each other
            st = state
            seq, prev = (store.head_seq(uid), store.log_head(uid)) if view is not None else (0, None)
            stamp = store.peek_stamp(uid)
            fail: str | None = None
            for s in todo:
                n += 1
                ev = self._event(s, st.tickets.get(uid), uid)
                probe = {
                    "v": 2,
                    "id": canon_ulid(),
                    "actor": {"kind": "person", "id": self.person, "device": self.device},
                    "auth": self.h.backend.auth,
                    "hash_v": 1,
                    "roster_v": st.workspace.roster_v,
                    "based_on": prev,
                    "prev": prev,
                    "seq": seq + 1,
                    "at": _fmt(clock + n),
                    "ws_seq": stamp["ws_seq"],
                    "sig": crypto.b64u(b"\0" * 64),
                    **ev,
                }
                r = preview(st, probe, log=uid)
                if isinstance(r, Refusal):
                    fail = f"{s.kind}: {r.code.value}: {r.detail}"[:200]
                    break
                try:
                    from orch import schema

                    schema.validate("event", {**probe, "host_sig": crypto.b64u(b"\0" * 64)}, log="ticket")
                    if s.body is not None:
                        schema.validate("body", {"type": tp.ticket_type, "sections": s.body})
                except Exception as e:
                    fail = f"{s.kind}: {e}"[:200]
                    break
                st, seq, prev = r, seq + 1, canon.event_head(probe)
            if fail is not None:
                out.skipped.append((t.key, fail))
                continue
            state = st
            known.add(t.key)
            out.work.append(Work(tp, todo, view is not None))
        return out

    def _complete(self, uid: str, tp: TicketPlan) -> bool:
        text = complete_text(tp.marker_digest)
        return any(e["type"] == "log.added" and e.get("text") == text for e in self.store.events(uid))

    def _event(self, s: Step, view: Any, uid: str) -> dict[str, Any]:
        """The event of step ``s`` as it is signed: with ``base_rev`` and the question's derived ids, read from
        ``view`` (the state the event is appended to)."""
        ev = dict(s.event)
        if s.kind == "update":
            paths = [*ev.get("set", {}), *(f"body.{sid}" for sid in ev.get("sections", {}))]
            ev["base_rev"] = {p: path_hash(view, p) for p in paths}
        elif s.kind == "question":
            q = ev["question"]
            wid = self.store.state.workspace.workspace_id
            qid = canon.question_id(wid, uid, q["id"])
            ev["qid"] = qid
            ev["hash"] = canon.question_hash(qid, uid, q["text"], q.get("options", []))
        return ev

    # -- doing
    def execute(self, plan: Plan, sign: Any) -> tuple[int, int, list[tuple[str, str]]]:
        """Append every step of every planned ticket. ``sign(payload) -> signature``. Returns (tickets finished,
        events appended, tickets left partial with the reason)."""
        done_tickets, events = 0, 0
        partial: list[tuple[str, str]] = []
        for w in plan.work:
            uid = w.plan.uid
            try:
                for s in w.todo:
                    view = self.store.state.tickets.get(uid)
                    if view is not None and _satisfied(s, view):
                        continue
                    ev = self._event(s, view, uid)
                    data: dict[str, bytes] | None = None
                    if s.data is not None:
                        raw = s.data if isinstance(s.data, bytes) else s.data.read()
                        data = {s.name or "": raw}
                    self._append(ev, uid, s.body, data, sign)
                    events += 1
                done_tickets += 1
            except Exception as e:
                partial.append((w.plan.key, f"{e}"[:200]))
        return done_tickets, events, partial

    def _append(
        self, ev: dict[str, Any], uid: str, body: dict[str, str] | None, files: dict[str, bytes] | None, sign: Any
    ) -> None:
        full = self.h._envelope(ev, uid, self.person, self.device)
        full["sig"] = crypto.b64u(sign(canon.person_signing_bytes(self.h.workspace_id, uid, full)))
        self.store.append(full, log=uid, body=body, artifacts=files)


def _satisfied(s: Step, view: Any) -> bool:
    """Does the ticket already hold what step ``s`` writes? (Only for finishing an interrupted import.)"""
    k, ev = s.kind, s.event
    if k == "created":
        return view is not None
    if view is None:
        return False
    if k in ("marker", "artifact"):
        return any(a.name == ev["name"] and a.digest == ev["sha256"] for a in view.artifacts)
    if k == "update":
        for path, val in ev.get("set", {}).items():
            if thaw(view.fields[path[len("ticket.") :]]) != val:
                return False
        return all(
            view.sections.get(sid, {}).get("hash") == entry["hash"] for sid, entry in ev.get("sections", {}).items()
        )
    if k == "question":
        return any(q.id == ev["question"]["id"] for q in view.questions)
    if k == "status":
        return view.status != "open"
    if k == "close":
        return view.status == "closed"
    return False  # the completion note: never satisfied by state, only by itself


def _epoch(stamp: str) -> int:
    import calendar

    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def _fmt(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def canon_ulid() -> str:
    from orch.identity import new_ulid

    return new_ulid()


# ------------------------------------------------------------------------------------------------ the operation


def run_import(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "import.v1")
    home = find_v1(args["path"])
    if home is None:
        raise OrchError(
            "not_found",
            f"no v1 workspace at {clean_text(args['path'], one_line=True)[:80]}",
            hint="the folder with orchestrator/config.json (schema 1)",
        )
    v1 = read_v1(home)
    imp = Importer(h, v1)
    plan = imp.plan()
    lines = _report(plan)
    count = len(plan.work)
    if ctx.dry_run:
        lines.insert(0, "dry-run: nothing was written")
        return _result(plan, count, plan.events, [], lines, "orch list")
    if not plan.work:
        return _result(plan, 0, 0, [], lines, "orch list")
    review = _review(plan, v1, imp)
    if not human.review_prompt("\n".join(review), f"IMPORT {count}"):
        raise OrchError("invalid.input", "not confirmed: nothing was signed")
    backend = h.backend
    fields = (
        ("import", f"{count} tickets from v1, {plan.events} events"),
        ("workspace", v1.prefix),
        ("plan", plan.digest()),
    )
    unlock = getattr(backend, "unlocked", None)
    if unlock is not None:
        with unlock(KEY_ID, action="import v1", digest=plan.digest().split(":")[1][:32], fields=fields) as sign:
            ok, events, partial = imp.execute(plan, sign)
    else:  # a backend that cannot unlock once signs (and asks) per event
        ok, events, partial = imp.execute(plan, lambda payload: backend.sign(KEY_ID, payload, action="import v1"))
    if partial:
        lines += [f"partial {k}: {why}" for k, why in partial[:_SHOWN]]
    return _result(plan, ok, events, partial, lines, "run the same command again to finish" if partial else "orch list")


def _report(plan: Plan) -> list[str]:
    lines = [f"{len(plan.work)} to import, {len(plan.already)} already imported, {len(plan.skipped)} skipped"]
    for key, why in plan.skipped[:_SHOWN]:
        lines.append(f"skipped {key}: {why}")
    if len(plan.skipped) > _SHOWN:
        lines.append(f"+{len(plan.skipped) - _SHOWN} more skipped")
    for a in plan.already[:3]:
        lines.append(f"already {a}")
    warns = sum(len(w.plan.warnings) for w in plan.work)
    left = sum(len(w.plan.left_out) for w in plan.work)
    if warns or left:
        lines.append(f"{warns} text repairs, {left} things kept only in v1-import.json (see each ticket's artifact)")
    return lines


def _review(plan: Plan, v1: V1Workspace, imp: Importer) -> list[str]:
    by_status: dict[str, int] = {}
    for w in plan.work:
        by_status[w.plan.v1_status] = by_status.get(w.plan.v1_status, 0) + 1
    out = [
        f"import {len(plan.work)} tickets from the v1 workspace {v1.prefix} ({v1.customer[:40]!r}),",
        f"{plan.events} signed events",
        "v1 status: " + ", ".join(f"{k} {n}" for k, n in sorted(by_status.items())),
        "what v2 gets: the tickets with their text, criteria, tasks (not their state) and files; the v1 ticket and its",
        "history as one read-only file per ticket. What it does not get: approvals, verdicts, evidence links, task",
        "state, commands, claims. A done ticket becomes closed; every other one starts open (backlog stays backlog).",
        f"you sign as {imp.person}; one passphrase covers the whole batch; plan {plan.digest()[:19]}",
        "",
    ]
    for w in plan.work[:_SHOWN]:
        out.append("| " + canon.clean(f"{w.plan.key} [{w.plan.v1_status}] {w.plan.title}")[:100])
    if len(plan.work) > _SHOWN:
        out.append(f"+{len(plan.work) - _SHOWN} more")
    out.append("")
    return out


def _result(plan: Plan, ok: int, events: int, partial: list[tuple[str, str]], lines: list[str], hint: str) -> Result:
    return Result(
        data={
            "count": ok,
            "already": len(plan.already),
            "skipped": len(plan.skipped),
            "events": events,
            "partial": len(partial),
        },
        hints=[hint],
        lines=lines,
        exit=5 if partial else 0,
    )
