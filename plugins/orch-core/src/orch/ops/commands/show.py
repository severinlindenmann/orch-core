"""orch show: read a ticket, a section, the log or a diff"""

import json
from typing import Any

from orch.ops import views
from orch.ops._dsl import REF, SECTIONS, STR, B, I, L, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call
from orch.ops.views import fence
from orch.schema import SECTIONS_BY_TYPE
from orch.store.render import HEADINGS, thaw

LAST = 5  # events in the default view
LOG_DEFAULT = 20


def _questions(view: Any) -> list[dict[str, Any]]:
    by_id = {q["id"]: q for q in view.fields["questions"]}
    out = []
    for q in view.questions:
        doc = by_id.get(q.id, {})
        row: dict[str, Any] = {"id": q.id, "to": q.to, "blocking": q.blocking, "answered": q.answered}
        row["text"] = doc.get("text", "")
        if doc.get("options"):
            row["options"] = [o["key"] for o in doc["options"]]
        if doc.get("recommended"):
            row["recommended"] = doc["recommended"]
        out.append(row)
    return out


def _document(
    view: Any, texts: dict[str, str], events: list[dict[str, Any]], sections: tuple[str, ...]
) -> dict[str, Any]:
    """The ticket document of F1 section 7 for ``--json``: the fields and what is derived from the events."""
    doc: dict[str, Any] = {
        "key": view.key,
        "title": view.title,
        "type": view.type,
        "status": view.status,
        "priority": view.fields["priority"],
        "size": view.fields["size"],
        "labels": list(view.fields["labels"]),
        "due": view.fields["due"],
        "parent": view.fields["parent"],
        "blocked_by": list(view.fields["blocked_by"]),
        "owner": view.owner,
        "claim": {"session": view.claim.session, "live": view.claim.live} if view.claim else None,
        "waiting": view.waiting,
        "acceptance": [{"id": a.id, "text": a.text, "evidence": list(a.evidence)} for a in view.acceptance],
        "tasks": [{"id": t.id, "text": t.text, "state": t.state, "proves": list(t.proves)} for t in view.tasks],
        "questions": _questions(view),
        "current_state": texts.get("current_state", ""),
        "events": [views.event_line(e) for e in events],
    }
    if sections:
        doc["sections"] = {s: texts.get(s, "") for s in sections}
    return doc


def _header(c: Call, view: Any) -> str:
    bits = [view.key, view.type, view.status, view.fields["priority"]]
    if view.fields["size"]:
        bits.append(f"size={view.fields['size']}")
    if view.claim is not None:
        mine = c.ctx.session and view.claim.session in (c.ctx.session, c.ctx.session.rpartition(".")[0])
        bits.append("claim=you" if mine else "claim=other")
        if not view.claim.live:
            bits.append(f"lapsed={view.claim.lapsed}")
    if view.waiting:
        bits.append("waiting")
    if view.frozen:
        bits.append("frozen")
    return " ".join(bits)


def _default(c: Call, view: Any, head: int) -> tuple[list[str], dict[str, Any]]:
    texts = c.store.body_sections(view.uid)
    events = c.store.events(view.key, after=max(0, head - LAST))
    body = [f"title: {views.short(view.title, 100)}"]
    state = texts.get("current_state", "").strip()
    if state:
        body.append("current_state: " + views.short(state.replace("\n", " / "), 240))
    qs = {q["id"]: q for q in _questions(view)}
    for q in views.open_questions(view):
        body.append(views.question_line(q, qs))
    shown = view.acceptance[: views.MORE]
    body += [views.ac_line(a) for a in shown]
    if len(view.acceptance) > len(shown):
        body.append(f"+{len(view.acceptance) - len(shown)} more criteria (orch show --full)")
    tasks = view.tasks[: views.MORE]
    body += [views.task_line(t) for t in tasks]
    if len(view.tasks) > len(tasks):
        body.append(f"+{len(view.tasks) - len(tasks)} more tasks (orch task list)")
    if events:
        body += [views.event_line(e) for e in events]
    lines = [_header(c, view), *fence("\n".join(body), f"ticket {view.key}")]
    c.shown(view, fields=views.FIELDS_SHOWN, sections=("current_state",), seq=head)
    return lines, _document(view, texts, events, ())


def _sections(
    c: Call, view: Any, wanted: tuple[str, ...], head: int, *, full: bool
) -> tuple[list[str], dict[str, Any]]:
    texts = c.store.body_sections(view.uid)
    allowed = SECTIONS_BY_TYPE[view.type]
    bad = [s for s in wanted if s not in allowed]
    if bad:
        raise OrchError("invalid.input", f"a {view.type} ticket has no section {', '.join(bad)}")
    lines = [_header(c, view)]
    if full:
        qs = {q["id"]: q for q in _questions(view)}
        meta = [f"title: {views.short(view.title, 200)}"]
        meta += [views.question_line(q, qs) for q in views.open_questions(view)]
        meta += [views.ac_line(a, 200) for a in view.acceptance] + [views.task_line(t, 200) for t in view.tasks]
        meta.append(
            f"labels: {','.join(view.fields['labels']) or '-'}  "
            f"links: {views.short(json.dumps(thaw(view.fields['links'])), 160)}"
        )
        gates = [
            f"{g}:{'approved' if v.approved else ('waiting' if v.waiting else 'open')}"
            for g, v in view.gates.items()
            if v.applies
        ]
        meta.append("gates: " + " ".join(gates))
        lines += fence("\n".join(meta), f"ticket {view.key}")
    for sid in wanted:
        lines += fence(texts.get(sid, "") or "(empty)", f"section {sid}")
    c.shown(
        view,
        fields=views.ALL_FIELDS if full else (),
        sections=tuple(wanted),
        seq=head,
    )
    events = c.store.events(view.key, after=max(0, head - LAST))
    return lines, _document(view, texts, events, wanted)


def _events(c: Call, view: Any, head: int, args: dict[str, Any], *, diff: bool) -> tuple[list[str], dict[str, Any]]:
    since = args.get("since")
    if since is None:
        cur = c.cursor(view.uid)
        since = cur if diff and cur else max(0, head - LOG_DEFAULT)
    events = c.store.events(view.key, after=since)
    rows = []
    for e in events:
        line = views.event_line(e)
        if diff and e["type"] == "ticket.updated":
            paths = [*e.get("set", {}), *("body." + s for s in e.get("sections", {}))]
            line += " changed: " + ",".join(paths)
        rows.append(line)
    last = events[-1]["seq"] if events else since
    c.shown(view, seq=last)
    lines = [_header(c, view)]
    lines += fence("\n".join(rows), f"events after {since}") if rows else [f"no events after {since}"]
    return lines, {"key": view.key, "since": since, "events": rows}


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "show")
    modes = [m for m in ("section", "full", "log", "diff") if args.get(m)]
    if len(modes) > 1:
        raise OrchError("invalid.input", "give one of --section, --full, --log, --diff")
    if args.get("since") is not None and not (args.get("log") or args.get("diff")):
        raise OrchError("invalid.input", "--since goes with --log or --diff")
    view = c.resolve(args.get("ref"))
    head = c.store.head_seq(view.uid)
    name = modes[0] if modes else "default"
    if name == "section":
        lines, doc = _sections(c, view, tuple(args["section"]), head, full=False)
        shown = "sections"
    elif name == "full":
        lines, doc = _sections(c, view, tuple(h for h in HEADINGS if h in SECTIONS_BY_TYPE[view.type]), head, full=True)
        shown = "full"
    elif name in ("log", "diff"):
        lines, doc = _events(c, view, head, args, diff=name == "diff")
        shown = name
    else:
        lines, doc = _default(c, view, head)
        shown = "default"
    data: dict[str, Any] = {"view": shown}
    if shown in ("log", "diff"):
        data["ticket"] = doc
    else:
        data["ticket"] = doc
    return Result(
        data=data,
        key=view.key,
        seq=head,
        cursor=c.cursor(view.uid),
        hints=[views.next_hint(view, ctx.session)],
        lines=lines,
    )


OP = operation(
    "show",
    "Read",
    "Read a ticket: the default view, some sections, everything, the log or a diff since an event.",
    who="read",
    props={
        "ref": REF(),
        "section": L(
            "only these sections, comma separated", split=True, items={"enum": SECTIONS}, **{"x-metavar": "A,B"}
        ),
        "full": B("every section"),
        "log": B("the event log"),
        "diff": B("what changed"),
        "since": I("events after this seq", minimum=0, **{"x-metavar": "N"}),
    },
    positional=("ref",),
    pre=("ticket_exists", "ticket_visible"),
    text="ok {key} show {view} seq={seq}\nnext: {next}",
    data=obj({"view": STR, "ticket": {"type": "object"}}, optional=("ticket",)),
    errors=(err("not_found"), err("ambiguous_ref")),
    handler=handle,
)
