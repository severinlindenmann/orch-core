"""orch new: create a ticket"""

from typing import Any

from orch.ops import views
from orch.ops._dsl import FILE, LABEL_PATTERN, MSG, STR, E, L, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, flat, path_hash
from orch.schema import SECTIONS_BY_TYPE
from orch.store.render import section_entry


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "new")
    actor = c.actor() if ctx.prepare is None else None
    title = c.text(args["title"], one_line=True, what="title")
    if not 1 <= len(title) <= 200:
        raise OrchError("invalid.input", "title is 1 to 200 characters")
    kind = args.get("type", "feature")
    sets: dict[str, Any] = {}
    for k in ("priority", "size"):
        if args.get(k) and args[k] != ("medium" if k == "priority" else None):
            sets[f"ticket.{k}"] = args[k]
    if args.get("label"):
        sets["ticket.labels"] = sorted(set(args["label"]))
        from orch.ops import addon_gate, quick

        if quick.LABEL in sets["ticket.labels"]:
            addon_gate.require_on(c.store.state.workspace.addons, quick.ADDON)
    if args.get("parent"):
        parent = c.store.normalise_ref(args["parent"])
        pv = c.store.ticket(parent)
        if pv is None or not c.sees(pv):
            raise OrchError("not_found", f"no parent ticket {flat(args['parent'])[:40]}")
        sets["ticket.parent"] = pv.key
    text = c.read_text_source(args, ("message", "file"))
    summary = c.text(text, limit=65536, what="description").strip("\n") if text is not None else ""
    if summary:  # the summary is gate-signed: widget blocks are refused there, as `section set` does
        from orch.widgets import check

        problems = check.check_write({}, "summary", summary)
        if problems:
            raise OrchError("parse.text", check.message(problems, "summary"))
    if ctx.prepare is not None:
        return _prepare(ctx, c, title, kind, bool(sets or summary))
    if ctx.dry_run:
        view_key = c.store.peek_key()
        return Result(
            data={"title": title, "ticket_type": kind},
            key=view_key,
            seq=1,
            hints=[f"orch claim {view_key}"],
            lines=["dry-run: nothing was written"],
        )
    planned: dict[str, Any] = {}
    if ctx.plan is not None:  # host mode: the uid and every event id are fixed and announced before the append (§0.7)
        from orch.ops.runtime import canon_ulid

        planned = {"uid": canon_ulid(), "created": canon_ulid(), "updated": canon_ulid()}
        events = [(planned["uid"], planned["created"], "ticket.created", actor)]
        if sets or summary:
            events.append((planned["uid"], planned["updated"], "ticket.updated", actor))
        ctx.plan(events, None)
    made = c.store.create_ticket(
        actor=actor,
        ticket_type=kind,
        title=title,
        owner=actor["for"],
        idem=f"{ctx.idem}:new" if ctx.idem else None,
        uid=planned.get("uid"),
        event_id=planned.get("created"),
    )
    key, seq = made.event["key"], made.event["seq"]
    with c.locked():
        view = c.store.ticket(key)
        if sets or summary:
            # the creator knows the defaults it just made, so its edits are based on them
            p = c.projection(view)
            ev: dict[str, Any] = {"type": "ticket.updated"}
            if sets:
                ev["set"] = sets
            if summary:
                ev["sections"] = {"summary": section_entry(summary)}
            ev["base_rev"] = {path: path_hash(view, path) for path in sets}
            if summary:
                ev["base_rev"]["body.summary"] = path_hash(view, "body.summary")
            p.add(ev, body={"summary": summary} if summary else None)
            seq = p.commit(ids=[planned["updated"]] if planned else None)[-1].event["seq"]
            view = c.store.ticket(key)
        c.shown(view, fields=views.ALL_FIELDS, sections=tuple(SECTIONS_BY_TYPE[view.type]))
    return c.result(view, {"title": title, "ticket_type": kind}, seq=seq, hints=[f"orch claim {key}"])


def _prepare(ctx: Context, c: Call, title: str, kind: str, more: bool) -> Result:
    """``human.prepare`` of a person's ``new``: one ``ticket.created`` (``EMITS_V1["new"]``), with the key the store
    would allocate now. A key taken before the submit is refused there (``ticket.exists``): keys are never reused."""
    from orch.ops.prepare import Captured, judge
    from orch.ops.runtime import canon_ulid

    if more:
        raise OrchError(
            "invalid.input",
            "a person's new takes a title and a type (it emits ticket.created only); set the rest after",
        )
    capture = ctx.prepare
    key, uid = c.store.peek_key(), canon_ulid()
    event = {"type": "ticket.created", "key": key, "ticket_type": kind, "title": title, "owner": capture.person}
    judge(c.store, uid, event, capture)
    capture.add(Captured(uid, event))
    return Result(data={"title": title, "ticket_type": kind}, key=key, seq=0, hints=[f"orch show {key}"])


OP = operation(
    "new",
    "Lifecycle",
    "Create a ticket.",
    who="agent",
    props={
        "title": S("one line, at most 200 characters", maxLength=200, **{"x-metavar": "TITLE"}),
        "type": E("ticket type", "feature", "bug", "chore", "spike", "epic", default="feature"),
        "priority": E("priority", "low", "medium", "high", "urgent"),
        "size": E("size", "xs", "s", "m", "l", "xl"),
        "label": L(
            "labels, comma separated",
            split=True,
            items={"type": "string", "pattern": LABEL_PATTERN},
            **{"x-metavar": "A,B"},
        ),
        "parent": S("parent ticket key", **{"x-metavar": "KEY"}),
        "message": MSG,
        "file": FILE,
    },
    required=("title",),
    positional=("title",),
    pre=("workspace_exists", "grant_valid", "text_clean"),
    emits=("ticket.created",),
    text="ok {key} ticket.created {ticket_type} seq={seq}\nnext: {next}",
    data=obj({"title": STR, "ticket_type": STR}),
    errors=(err("not_found", "the parent does not exist", ["orch", "list"]), err("parse.text"), err("addon.off")),
    handler=handle,
)
