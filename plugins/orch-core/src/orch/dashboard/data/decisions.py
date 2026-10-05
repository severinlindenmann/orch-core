"""Decisions: everything needs_you() reports, enriched for the inline-action cards."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from orch.clock import now as clock_now
from orch.clock import parse_stamp
from orch.core import events as events_mod
from orch.core import query, store
from orch.core.artifacts import entries as artifact_entries
from orch.core.gates import GATE_SECTIONS, gate_hash, gate_meta, gate_parts
from orch.core.events import Actor
from orch.core.lifecycle import allowed_targets, unanswered_blocking
from orch.core.model import Ticket, parse_body, parse_ticket
from orch.dashboard.data import steps as steps_mod
from orch.dashboard.data import story as story_mod
from orch.dashboard.markdown import artifact_scope
from orch.errors import OrchError, TicketParseError

_HUMAN = Actor("human", "you", "dashboard")

CATEGORY = {"approve-requirements": "approvals", "approve-plan": "approvals", "re-approve": "approvals",
            "approve-epic": "approvals",
            "answer": "questions", "verdict": "verdicts", "broken": "broken", "task": "tasks",
            "stale-claim": "claims", "confirm": "questions"}
KIND_LABELS = {"approve-requirements": "Approve requirements", "approve-plan": "Approve plan",
               "re-approve": "Re-approve", "approve-epic": "Re-approve epic", "answer": "Answer", "verdict": "Verdict",
               "broken": "Repair", "task": "Your task", "stale-claim": "No activity", "confirm": "Confirm"}

_GATE_SECTION = {"requirements": "Requirements", "plan": "Plan"}
# Bullet markers and heading hashes are stripped; an ordered-list "1. " keeps its numbering, since
# plan/requirements steps are usually numbered on purpose.
_STRIP = re.compile(r"^\s*([-*+]|#+)\s+")
# Inline Markdown that would show as literal marks in the plain-text excerpt: code backticks and
# */_ emphasis runs at a word edge (so snake_case and file_name.py keep their underscores).
_INLINE = re.compile(r"`+|(?<!\w)[*_]+(?=\S)|(?<=\S)[*_]+(?!\w)")
_REF_DEF = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:")
_MAX_LINES = 6
_MAX_LEN = 160


def _excerpt(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _INLINE.sub("", _STRIP.sub("", raw_line.strip())).strip()
        if not line:
            continue
        lines.append(line[:_MAX_LEN])
        if len(lines) == _MAX_LINES:
            break
    return "\n".join(lines)


def _recommended_key(rec) -> str | None:
    if isinstance(rec, list):
        return str(rec[0]) if rec else None
    return str(rec) if rec not in (None, "") else None


def _question_dict(q: dict) -> dict:
    options = [{"key": str(o.get("key")), "label": str(o.get("label") or o.get("key") or "")}
               for o in (q.get("options") or []) if isinstance(o, dict)]
    recommended = _recommended_key(q.get("recommended"))
    if recommended is not None and any(o["key"] == recommended for o in options):
        options = sorted(options, key=lambda o: o["key"] != recommended)
    from orch.core.questions import question_hash
    return {
        "id": str(q.get("id") or ""),
        "hash": question_hash(q),  # posted as qhash: the answer is bound to the question text and options shown
        "text": str(q.get("text") or ""),
        "options": options,  # [{"key", "label"}], recommended first
        "recommended": recommended,
        "multi": q.get("type") == "multi",
    }


def _age_minutes(events_for_ticket: list, at_now: datetime) -> int | None:
    newest = None
    for ev in events_for_ticket:
        if ev.kind == "gate.invalidated":
            continue  # written by `orch check` when it notices, not when anything happened to the ticket
        try:
            at = parse_stamp(ev.at)
        except ValueError:
            continue
        if newest is None or at > newest:
            newest = at
    return None if newest is None else max(0, int((at_now - newest).total_seconds() // 60))


def _invalidated_age(t: Ticket, gate: str, events_for_ticket: list, at_now: datetime) -> int | None:
    """Minutes a re-approval has waited: since the first edit after the approval that could have changed what the
    gate binds (one of its sections, the Summary, a raw edit or a field edit), else since the approval itself."""
    approved = steps_mod.when(((t.meta.get("gates") or {}).get(gate) or {}).get("approved"))
    bound = set(GATE_SECTIONS.get(gate, ())) | {"Summary"}
    first = None
    for ev in events_for_ticket:
        data = ev.data if isinstance(ev.data, dict) else {}
        if ev.kind != "ticket.edited" or (data.get("section") is not None and data.get("section") not in bound):
            continue
        at = steps_mod.when(ev.at)
        if at is not None and (approved is None or at >= approved) and (first is None or at < first):
            first = at
    since = first or approved
    return None if since is None else max(0, int((at_now - since).total_seconds() // 60))


@dataclass
class Decision:
    kind: str
    category: str
    ticket: str
    title: str
    status: str
    gate: str | None          # "requirements" | "plan" for approvals (re-approve: the invalidated gate)
    excerpt: str              # plain text, <= 6 non-empty lines, each <= 160 chars
    questions: list[dict]     # for "answer": [{"id", "text", "options": [{"key","label"}], "recommended": str|None, "multi": bool}]
    age_minutes: int | None   # minutes since the newest event of the ticket; None if no event
    detail: str
    seen: str | None = None  # gate_hash of the gate's current text, for approval decisions (None otherwise)
    # An approval item that Ops.approve would refuse now (e.g. requirements changed after the
    # ticket left backlog): {"text", "action"} as steps.reapprove_hint gives it, shown instead of
    # an Approve button ("move" action: the real "Move back to ..." move; "hint": text only).
    hint: dict | None = None
    task: dict | None = None  # for "task": {"id", "text", "verify"}
    # Approval kinds: everything `seen` binds, shown in full before the button (#20). Sections as the file holds
    # them ([{"name", "text"}], hash order) and the frontmatter fields ([{"name", "value"}], hash v2: size, type).
    gate_parts: list[dict] | None = None
    gate_meta: list[dict] | None = None
    question: str | None = None  # a gate line that reads as an open question: Approve needs the override box ticked
    unsigned: list[str] | None = None  # approved gates of this ticket the ledger on this machine does not hold
    scope: str = "blocking"  # query.SCOPES: "blocking" | "later" | "backlog" (#11)
    # Grouping hook for Today: decisions with the same non-None key are drawn under one heading (data.today.groups).
    # E2 sets it to the epic's id for a ticket in an epic (or the epic itself), `group_label` to "Epic <id> · <title>".
    group: str | None = None
    group_label: str | None = None
    # An epic's approval (E2): its charter binds the epic and every child, so the card links to the epic page's
    # approve view instead of approving inline.
    charter: bool = False
    seen_short: str = ""  # "sha256 ab1e…7f" of `seen`, named by the inline confirm
    feedforward: str = ""  # rule text under the gated text: what the primary action does and does not do
    diff: list[dict] | None = None  # re-approve: the approved snapshot against the current text (story.diff)
    approved_day: str | None = None  # re-approve: when the gate was approved before
    criteria: list | None = None  # verdict: evidence.criteria(t), each AC with its proof lines
    verification: str = ""  # verdict: the Verification section, shown folded
    widgets: object = None  # verdict: the ticket's widgets (markdown.section_widgets), drawn as on the ticket page
    harness: str | None = None  # the claim's harness (stale-claim; the plan feedforward)
    priority: str | None = None
    # F2: requirements whose plan the agent drafted too: the plan as one more gated text ({"seen", "seen_short",
    # "gate_parts", "gate_meta", "question"}), approved with the requirements in one confirm. None otherwise.
    together: dict | None = None
    why: str = ""  # F4: one line saying why this waits on the human (rule text, never agent prose)
    art: object | None = None  # markdown.ArtifactScope: what `artifact:<name>` in the gated text and evidence shows
    artifacts: int = 0  # how many artifacts the ticket links

    @property
    def cid(self) -> str:
        """The card's id on the page (`d-<cid>`), also the focus view's `at=`: one per ticket and kind (per task)."""
        return f"{self.ticket}-{self.kind}" + (f"-{self.detail}" if self.kind == "task" else "")


def _repair(item: dict, at_age: int | None, message: str) -> Decision:
    return Decision(kind="broken", category=CATEGORY["broken"], ticket=item["ticket"], title=item.get("title") or "",
                    status=item["status"], gate=None, excerpt=message, questions=[], age_minutes=at_age, detail=message)


def _error_text(e: OrchError) -> str:
    return e.message + (f" — {e.hint}" if e.hint else "")


def decisions(ws, *, now: datetime | None = None, events: list | None = None,
              entries: list[store.Entry] | None = None, needs: list[dict] | None = None) -> list[Decision]:
    """Everything `needs` (query.waiting(), else needs_you()) reports, in the same order, enriched for the cards.

    `events`, `entries` (a `store.scan`) and `needs` (a `query.needs_you`) may be shared with the
    other parts of one request; each is computed here once when not given. Every item's ticket
    is parsed straight from its scanned file. An id held by two files, or a file that fails to
    parse, becomes one "Repair" card carrying the error instead of failing the page.
    """
    at_now = now or clock_now()
    all_events = events if events is not None else events_mod.read_events(ws)
    entries = store.scan(ws) if entries is None else entries
    needs = query.needs_you(ws, entries=entries) if needs is None else needs
    skip = tuple(ws.config["gates"]["plan_skip_sizes"])
    by_ticket: dict[str | None, list] = {}
    for ev in all_events:
        by_ticket.setdefault(ev.ticket, []).append(ev)
    by_id: dict[str, list[store.Entry]] = {}
    for e in entries:
        by_id.setdefault(e.id.upper(), []).append(e)

    from orch.core import ledger
    signed = ledger.entries(ws)  # one cached read for the whole request
    from orch.dashboard.data.epic import epic_index as _epic_index
    epic_index = _epic_index(entries)  # E2: Today groups decisions by epic
    out: list[Decision] = []
    repaired: set[str] = set()
    for item in needs:
        tid, kind = item["ticket"], item["kind"]
        if kind in query.FACTORY_KINDS:
            continue  # the factory's Ready and Stopped cards are drawn from orch.core.factory_report, not as a Decision
        if tid.upper() in repaired:
            continue  # one Repair card per id, however many items its files produced
        age = _age_minutes(by_ticket.get(tid, []), at_now)
        hits = by_id.get(tid.upper(), [])
        try:
            if len(hits) > 1:
                store.resolve(ws, tid, entries)  # raises the "is ambiguous" UsageError naming the files
            if kind == "broken":
                detail = item.get("detail") or ""
                out.append(Decision(kind=kind, category=CATEGORY[kind], ticket=tid, title=item.get("title") or "",
                                     status=item["status"], gate=None, excerpt=detail, questions=[],
                                     age_minutes=age, detail=detail))
                repaired.add(tid.upper())
                continue
            if not hits:
                continue  # vanished between the scan and here
            entry = hits[0]
            try:
                text = entry.path.read_text(encoding="utf-8")
                if entry.meta is not None:
                    # The scan already parsed and validated this file's frontmatter (cached by
                    # mtime); only the Markdown body is split here, no second YAML parse.
                    sections, preamble = parse_body(text)
                    t = Ticket(meta=entry.meta, sections=sections, preamble=preamble)
                else:
                    t = parse_ticket(text, entry.path.relative_to(ws.home).as_posix())
            except FileNotFoundError:
                continue  # moved or deleted since the scan
            except UnicodeDecodeError as e:
                raise TicketParseError(f"{entry.path.name}: not UTF-8 ({e})") from e
        except OrchError as e:
            out.append(_repair(item, age, _error_text(e)))
            repaired.add(tid.upper())
            continue

        gate: str | None = None
        excerpt = ""
        questions: list[dict] = []
        task_info = None
        detail = item.get("detail") or ""
        charter = t.meta.get("type") == "epic" and kind in ("approve-requirements", "approve-epic")
        if charter:
            from orch.core.epics import children
            kids = [c.id for c in children(ws, t.id, entries) if c.status != "done"]
            gate = "requirements"
            excerpt = (f"The epic and {len(kids)} {'child' if len(kids) == 1 else 'children'}: " + ", ".join(kids)
                       if kids else "The epic, no children yet.")
            if kind == "approve-epic":
                excerpt = "Changed since your approval: " + (detail or "the epic's own text") + "\n" + excerpt
        elif kind == "approve-requirements":
            gate, excerpt = "requirements", _excerpt(t.section("Requirements"))
        elif kind == "approve-plan":
            gate, excerpt = "plan", _excerpt(t.section("Plan"))
        elif kind == "re-approve":
            gate = detail
            section = _GATE_SECTION.get(gate)
            excerpt = _excerpt(t.section(section)) if section else ""
        elif kind == "verdict":
            verification = t.section("Verification").strip()
            excerpt = _excerpt(verification) if verification else "No Verification section yet."
        elif kind == "answer":
            questions = [_question_dict(q) for q in unanswered_blocking(t)]
        elif kind == "confirm":
            questions = [_question_dict(q) for q in t.meta.get("questions") or [] if isinstance(q, dict)
                         and not q.get("blocking", True) and q.get("answer") in (None, "")]
        elif kind == "task":
            from orch.core import tasks as tk
            try:
                found = tk.find(tk.ticket_tasks(t), detail)
                task_info = {"id": found.id, "text": found.text, "verify": found.verify}
                excerpt = found.text + (f"\nVerify: {found.verify}" if found.verify else "")
            except OrchError:
                task_info = {"id": detail, "text": "", "verify": None}

        seen = gate_hash(t, gate) if gate in GATE_SECTIONS and not charter else None
        if kind == "verdict":  # the verdict binds the criteria, Verification and status the card shows
            from orch.core.epics import verdict_hash
            seen = verdict_hash([t], ws)
        parts = meta = None
        if gate in GATE_SECTIONS and not charter:
            # A Markdown link reference definition (`[x]: url`) renders as nothing but is hashed: such a section
            # is shown as its raw text, so nothing the approval binds is invisible.
            parts = [{"name": name, "text": t.section(name), "raw": bool(_REF_DEF.search(t.section(name)))}
                     for name, _ in gate_parts(t, gate)]
            meta = [{"name": key, "value": value} for key, value in gate_meta(t, gate)]
        hint = None
        question = None
        if gate in GATE_SECTIONS:
            from orch.core.gates import human_questions_in
            question = (human_questions_in(t, gate) or [None])[0]
        if gate in GATE_SECTIONS and not charter and not steps_mod.can_approve(t, gate, plan_skip_sizes=skip,
                                                                               allow_override=True):
            # Never offer an Approve that Ops.approve refuses: offer the real move (ticket page rule).
            moves = allowed_targets(t, _HUMAN, plan_skip_sizes=skip,
                                    open_blockers=query.open_blockers(ws, t, entries))
            hint = steps_mod.reapprove_hint(t, gate, moves)
        together = None
        if kind == "approve-requirements" and item.get("together") and not charter and hint is None:
            from orch.core.gates import human_questions_in
            plan_seen = gate_hash(t, "plan")
            together = {"seen": plan_seen, "seen_short": story_mod.short_hash(plan_seen),
                        "gate_parts": [{"name": name, "text": t.section(name),
                                        "raw": bool(_REF_DEF.search(t.section(name)))}
                                       for name, _ in gate_parts(t, "plan")],
                        "gate_meta": [{"name": k, "value": v} for k, v in gate_meta(t, "plan")],
                        "question": (human_questions_in(t, "plan") or [None])[0],
                        # every line of both gates that reads as an open question: the one override covers them all
                        "questions": human_questions_in(t, "requirements") + human_questions_in(t, "plan")}
        claim = t.meta.get("claim") if isinstance(t.meta.get("claim"), dict) else {}
        harness = item.get("harness") or (str(claim.get("harness")) if claim.get("session") else None)
        diff = approved_day = criteria = None
        if kind == "re-approve" and gate in GATE_SECTIONS:
            from orch.core.gates import approved_snapshot, normalized_text
            age = _invalidated_age(t, gate, by_ticket.get(tid, []), at_now)
            diff = story_mod.diff(approved_snapshot(ws, t.id, gate), normalized_text(t, gate))
            approved_day = steps_mod.day(((t.meta.get("gates") or {}).get(gate) or {}).get("approved"))
        widgets = None
        if kind == "verdict":
            from orch.core import evidence
            from orch.dashboard.markdown import section_widgets
            criteria = evidence.criteria(t)
            widgets = section_widgets(ws, t, assets=True)
        out.append(Decision(kind=kind, category=CATEGORY.get(kind, "broken"), ticket=t.id, title=t.title,
                             status=item["status"], gate=gate, excerpt=excerpt, questions=questions,
                             age_minutes=age, detail=detail, seen=seen, hint=hint, task=task_info,
                             gate_parts=parts, gate_meta=meta, question=question,
                             unsigned=ledger.unsigned_gates(ws, t, signed), scope=item.get("scope", "blocking"),
                             seen_short=story_mod.short_hash(seen) if seen else "",
                             feedforward=(_TOGETHER_FF.format(tid=t.id) if together
                                          else _feedforward(kind, gate, t.id, harness)),
                             together=together, why=steps_mod.why_waiting(item, together=bool(together)),
                             diff=diff, approved_day=approved_day,
                             criteria=criteria, verification=t.section("Verification") if kind == "verdict" else "", widgets=widgets,
                             harness=harness, priority=t.meta.get("priority"), charter=charter,
                             art=artifact_scope(t, ws), artifacts=len(artifact_entries(t)),
                             **_epic_group(ws, t, epic_index)))
    return out


_TOGETHER_FF = ("Approving both moves {tid} to open with its plan approved, so an agent can claim it and work the "
                "tasks. Does not accept the work.")


def _feedforward(kind: str, gate: str | None, tid: str, harness: str | None) -> str:
    """What the card's primary action does, and what it does not (rule text, never agent prose)."""
    who = harness or "the agent"
    if kind == "approve-requirements" or (kind == "re-approve" and gate == "requirements"):
        return (f"Approving moves {tid} to open, so an agent can claim it. Does not approve a plan."
                if kind == "approve-requirements" else "Approves the changed requirements again.")
    if kind == "approve-plan":
        return f"Lets {who} work the tasks. Does not accept the work."
    if kind == "re-approve":
        return f"Approves the changed plan again; {who} goes on with the tasks."
    if kind == "verdict":
        return f"Accept closes {tid} as done. Send back moves it to in progress with your note."
    if kind == "stale-claim":
        return f"Releasing puts {tid} back to open; any agent can claim it. Branch and notes stay."
    if kind == "confirm":
        return f"{who} went ahead with the recommendation. Confirm it or pick another option."
    return ""


_GATE_KINDS = ("approve-requirements", "approve-plan", "re-approve", "approve-epic")


def _epic_group(ws, t, index: dict) -> dict:
    """Today's group of a decision (E2): the epic the ticket is in, or the epic itself; none outside an epic."""
    from orch.core.epics import is_epic
    from orch.dashboard.data.epic import epic_of
    e = index.get(t.id.upper()) if is_epic(t) else epic_of(ws, t.meta, index)
    return {"group": e["id"], "group_label": f"Epic {e['id']} · {e['title']}"} if e else {}


def card_anchor(d: Decision, qid: str | None = None) -> str | None:
    """The PendingDecision anchor a decision card offers: a question of an answer card, its gate, or the verdict."""
    if d.kind == "answer":
        return qid
    if d.kind in _GATE_KINDS and d.gate:
        return f"gate:{d.gate}"
    return "verdict" if d.kind == "verdict" else None


def place_addon_decisions(items, cards) -> tuple[dict, list]:
    """Split addon decisions into those drawn on a shown card, keyed (TICKET, anchor), and the rest (From addons)."""
    spots = set()
    for c in cards:
        anchors = [str(q.get("id")) for q in c.questions] if c.kind == "answer" else [card_anchor(c)]
        spots.update((c.ticket.upper(), a) for a in anchors if a)
    inline: dict = {}
    loose: list = []
    for addon, d in items:
        key = (str(d.ticket or "").strip().upper(), d.anchor)
        if d.anchor and key in spots:
            inline.setdefault(key, []).append((addon, d))
        else:
            loose.append((addon, d))
    return inline, loose
