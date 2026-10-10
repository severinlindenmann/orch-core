"""orch new: create a ticket"""

from typing import Any

from orch.canon.text import clean_line
from orch.ops import views
from orch.ops._dsl import FILE, LABEL_PATTERN, MSG, STR, E, L, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, path_hash
from orch.schema import SECTIONS_BY_TYPE
from orch.store.render import section_entry


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "new")
    actor = c.actor()
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
    if args.get("parent"):
        parent = c.store.normalise_ref(args["parent"])
        pv = c.store.ticket(parent)
        if pv is None or not c.sees(pv):
            raise OrchError("not_found", f"no parent ticket {clean_line(args['parent'])[:40]}")
        sets["ticket.parent"] = pv.key
    text = c.read_text_source(args, ("message", "file"))
    summary = c.text(text, limit=65536, what="description").strip("\n") if text is not None else ""
    if ctx.dry_run:
        view_key = c.store.peek_key()
        return Result(
            data={"title": title, "ticket_type": kind},
            key=view_key,
            seq=1,
            hints=[f"orch claim {view_key}"],
            lines=["dry-run: nothing was written"],
        )
    made = c.store.create_ticket(
        actor=actor,
        ticket_type=kind,
        title=title,
        owner=actor["for"],
        idem=f"{ctx.idem}:new" if ctx.idem else None,
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
            seq = p.commit()[-1].event["seq"]
            view = c.store.ticket(key)
        c.shown(view, fields=views.ALL_FIELDS, sections=tuple(SECTIONS_BY_TYPE[view.type]))
    return c.result(view, {"title": title, "ticket_type": kind}, seq=seq, hints=[f"orch claim {key}"])


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
    errors=(err("not_found", "the parent does not exist", ["orch", "list"]), err("parse.text")),
    handler=handle,
)
