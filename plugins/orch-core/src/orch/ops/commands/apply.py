"""orch apply: apply an atomic batch"""

from typing import Any

from jsonschema import Draft202012Validator

import orch.ops as ops
from orch import canon
from orch.cli.store_errors import to_orch_error
from orch.ops import plans, views
from orch.ops._dsl import FILE, INT, STR, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, flat
from orch.store import StoreError

MAX_ITEMS = 100
# keys a batch item may carry where the operation has more than the batch can do (--run, --artifact, --ac)
ITEM_KEYS = {"task.done": {"task", "message"}}


def _items(c: Call, args: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    raw = c.read_file_text(args["file"])
    try:
        doc = canon.loads_strict(raw.encode("utf-8"))
    except (canon.JcsError, ValueError):
        raise OrchError("parse.json", "the batch is not strict JSON (no duplicate keys, no floats)") from None
    if not isinstance(doc, dict) or set(doc) - {"ref", "ops"} or not isinstance(doc.get("ops"), list):
        raise OrchError("invalid.input", 'the batch is {"ref": "DEMO-0043", "ops": [{"op": "log", "text": "..."}]}')
    ref = doc.get("ref")
    if ref is not None and not isinstance(ref, str):
        raise OrchError("invalid.input", "ref is a ticket REF")
    if not 1 <= len(doc["ops"]) <= MAX_ITEMS:
        raise OrchError("invalid.input", f"a batch holds 1 to {MAX_ITEMS} operations")
    return ref, doc["ops"]


def _check(item: Any, i: int, table: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One item as ``(operation name, arguments)``, validated against that operation's own input schema."""
    if not isinstance(item, dict) or not isinstance(item.get("op"), str):
        raise OrchError("invalid.input", f'item {i}: an object with an "op"')
    name = item["op"]
    if name not in table:
        raise OrchError("invalid.input", f"item {i}: {flat(name)[:40]!r} is not a batch operation")
    args = {k: v for k, v in item.items() if k != "op"}
    for banned in ("ref", "file"):
        if banned in args:
            raise OrchError("invalid.input", f"item {i}: {banned} is not allowed in a batch item (ref is the batch's)")
    allowed = ITEM_KEYS.get(name)
    if allowed is not None and set(args) - allowed:  # a key the batch cannot honour is refused, never ignored
        extra = ", ".join(sorted(set(args) - allowed))
        raise OrchError("invalid.input", f"item {i} ({name}): {extra} is not supported in a batch")
    props = dict(ops.get(name).input)
    props["required"] = [r for r in props.get("required", []) if r != "ref"]  # the batch names the ticket
    errors = sorted(Draft202012Validator(props).iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(str(p) for p in errors[0].absolute_path)
        raise OrchError("invalid.input", f"item {i} ({name}): {where or 'arguments'} is not valid")
    return name, args


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    c = Call.of(ctx, "apply")
    ref, items = _items(c, args)
    table = plans.batch_ops()
    checked = [_check(item, i, table) for i, item in enumerate(items, 1)]
    for name, _ in checked:
        c.check_verb(name)  # a grant that lists operations must list every operation the batch runs
    with c.locked():
        view = c.resolve(ref)
        if ctx.idem and not ctx.dry_run:
            started = [i for i in range(len(checked) + 4) if c.store.attempt_logged(view.uid, f"{ctx.idem}:{i}")]
            if started:
                raise OrchError(
                    "invalid.input",
                    f"an earlier attempt of this batch was interrupted after {len(started)} event(s); "
                    "orch show --log shows what was written: send only the rest",
                    hint="orch show --log",
                )
        if any(name.startswith("task.") for name, _ in checked):
            c.require_claim(view)
        p = c.projection(view)
        for i, (name, a) in enumerate(checked, 1):
            try:
                table[name][1](c, p, a)
            except StoreError as e:
                e = to_orch_error(e, c.declared)
                raise OrchError(e.code, f"item {i} ({name}): {e.message}", retryable=e.retryable) from None
            except OrchError as e:
                raise OrchError(
                    e.code, f"item {i} ({name}): {e.message}", hint=e.hint, fix=e.fix, retryable=e.retryable
                ) from None
        done = p.commit()
        seq = done[-1].event["seq"] if done else p.last_seq
        types = [x.event["type"] for x in p.planned]
        return c.result(
            p.last_view,
            {"count": len(types), "events": types},
            seq=seq,
            hints=[views.next_hint(p.last_view, ctx.session)],
        )


OP = operation(
    "apply",
    "Edit",
    "Apply a batch of operations atomically, from JSON (--file -).",
    who="agent",
    props={"file": FILE},
    required=("file",),
    pre=("ticket_exists", "base_rev_tracked", "grant_valid", "text_clean"),
    emits=(
        "ticket.updated",
        "log.added",
        "task.started",
        "task.done",
        "task.skipped",
        "task.blocked",
        "task.reopened",
        "artifact.added",
        "artifact.replaced",
        "question.asked",
    ),
    text="ok {key} apply {count} seq={seq}\nnext: {next}",
    data=obj({"count": INT, "events": arr(STR)}),
    errors=(
        err("parse.json"),
        err("parse.text"),
        err("claim.required"),
        err("lease.held"),
        err("conflict.section"),
        err("conflict.field"),
        err("transition.refused"),
        err("not_found"),
    ),
    handler=handle,
)
