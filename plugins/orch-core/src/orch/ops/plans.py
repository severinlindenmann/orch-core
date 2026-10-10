"""How each editing operation turns its arguments into events, written once and used by the operation (one call, one
ticket) and by ``apply`` (many items, one ticket, all or nothing).

A builder takes ``(call, projection, args)``, adds its events to the projection (which judges each one with the model)
and returns an :class:`Out`: the ``data`` of the result, the hints and the extra lines. ``run`` is the standard frame:
under the workspace lock, resolve the ticket, check the claim, build, append, answer.
"""

from __future__ import annotations

import datetime
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orch import canon
from orch.cli.render import fence
from orch.ops import views
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Projection, short
from orch.store.render import section_entry, thaw

ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TO_ROLES = ("ticket_owner", "assignees", "reviewers", "watchers")
_PERSON = re.compile(r"p_[0-9a-f]{32}")
_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-(?:(?!0000)[0-9]{4}|[1-9][0-9]{4,})")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_KIND = {
    ".log": "log",
    ".txt": "log",
    ".png": "screenshot",
    ".jpg": "screenshot",
    ".jpeg": "screenshot",
    ".gif": "screenshot",
    ".webp": "screenshot",
    ".md": "report",
    ".html": "report",
    ".pdf": "report",
}


@dataclass
class Out:
    data: dict[str, Any] = field(default_factory=dict)
    hints: list[str] | None = None
    lines: list[str] = field(default_factory=list)


Builder = Callable[[Call, Projection, dict[str, Any]], Out]


def run(
    ctx: Context,
    op: str,
    args: dict[str, Any],
    build: Builder,
    *,
    claim: bool = False,
    ref_key: str = "ref",
    live_only: bool = True,
) -> Result:
    """The frame of an editing operation: lock, resolve the ticket, (check the claim), build, append, answer."""
    c = Call.of(ctx, op)
    with c.locked():
        view = c.resolve(args.get(ref_key), live_only=live_only)
        if claim:
            c.require_claim(view)
        p = c.projection(view)
        out = build(c, p, args)
        done = p.commit()
        seq = done[-1].event["seq"] if done else p.last_seq
        hints = out.hints if out.hints is not None else [views.next_hint(p.last_view, ctx.session)]
        return c.result(p.last_view, out.data, seq=seq, hints=hints, lines=out.lines)


def unknown_task(view: Any, tid: str) -> OrchError:
    return OrchError("not_found", f"{view.key} has no task {tid}")


# ----------------------------------------------------------------------------------------------------- simple events


def log(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.body_text(c.read_text_source(args) or "", what="note")
    p.add({"type": "log.added", "text": text})
    return Out({}, ["orch wait" if not c.attended else views.next_hint(p.last_view, c.ctx.session)])


def _next_id(prefix: str, existing: list[str]) -> str:
    return f"{prefix}{max((int(x[len(prefix) :]) for x in existing), default=0) + 1}"


def ask(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.text(args["text"], what="question")
    opts = list(args.get("options") or [])
    if len(set(opts)) != len(opts):
        raise OrchError("invalid.input", "an option key is given twice")
    rec = args.get("rec")
    if rec is not None and rec not in opts:
        raise OrchError("invalid.input", "--rec must be one of --options")
    to = args.get("to") or "ticket_owner"
    if to not in TO_ROLES and not _PERSON.fullmatch(to):
        raise OrchError("invalid.input", "--to is a person id or one of " + ", ".join(TO_ROLES))
    qid_n = _next_id("Q", [q["id"] for q in p.last_view.fields["questions"]])
    options = [{"key": k, "label": k} for k in opts]
    q: dict[str, Any] = {"id": qid_n, "to": to, "text": text, "blocking": not args.get("non_blocking")}
    if args.get("why"):
        q["why"] = c.text(args["why"], what="why")
    if options:
        q["options"] = options
    if rec is not None:
        q["recommended"] = rec
    wid = c.store.state.workspace.workspace_id
    qid = canon.question_id(wid, p.uid, qid_n)
    p.add({"type": "question.asked", "question": q, "qid": qid, "hash": canon.question_hash(qid, p.uid, text, options)})
    return Out({"question": qid_n, "blocking": q["blocking"]}, ["orch wait" if q["blocking"] else "orch task next"])


def section_set(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    sid = args["section"]
    text = c.text(c.read_text_source(args, ("message", "file")) or "", limit=65536, what=f"section {sid}").strip("\n")
    p.add(
        {
            "type": "ticket.updated",
            "base_rev": {f"body.{sid}": p.base(f"body.{sid}")},
            "sections": {sid: section_entry(text)},
        },
        body={sid: text},
    )
    return Out({"section": sid})


# ----------------------------------------------------------------------------------------------------- ticket fields


def _none(v: str) -> bool:
    return v in ("none", "null")


def _keys(c: Call, p: Projection, v: str, what: str) -> list[str]:
    if _none(v):
        return []
    out = []
    for item in v.split(","):
        key = c.store.normalise_ref(item)
        if not _KEY.fullmatch(key):
            raise OrchError("invalid.input", f"{what}: {short(item, 40)!r} is not a ticket key")
        other = c.store.ticket(key)  # loads it: the model judges the reference against its place in the order
        if other is None or not c.sees(other):
            raise OrchError("not_found", f"{what}: no ticket {key}")
        out.append(key)
    if len(set(out)) != len(out):
        raise OrchError("invalid.input", f"{what}: a key is given twice")
    p.refresh()  # the referenced tickets are loaded now: judge against a state that has them
    return out


def parse_pair(c: Call, p: Projection, key: str, raw: str) -> Any:
    """The new value of ``ticket.<key>`` from the text after ``=`` (``none`` clears size, due, parent and the lists)."""
    enums = {
        "priority": ("low", "medium", "high", "urgent"),
        "size": ("xs", "s", "m", "l", "xl"),
    }
    if key == "title":
        t = c.text(raw, one_line=True, what="title")
        if not 1 <= len(t) <= 200:
            raise OrchError("invalid.input", "title is 1 to 200 characters")
        return t
    if key in enums:
        if key == "size" and _none(raw):
            return None
        if raw not in enums[key]:
            raise OrchError("invalid.input", f"{key} is one of {', '.join(enums[key])}")
        return raw
    if key == "labels":
        labels = [] if _none(raw) else raw.split(",")
        if any(not _LABEL.fullmatch(x) for x in labels) or len(set(labels)) != len(labels):
            raise OrchError("invalid.input", "labels: lower-case tokens, comma separated, no repeats")
        return labels
    if key == "due":
        if _none(raw):
            return None
        try:
            if not _DAY.fullmatch(raw):
                raise ValueError(raw)
            datetime.date.fromisoformat(raw)
        except ValueError:
            raise OrchError("invalid.input", "due is YYYY-MM-DD") from None
        return raw
    if key == "parent":
        got = _keys(c, p, raw, "parent")
        if len(got) > 1:
            raise OrchError("invalid.input", "parent is one ticket key")
        return got[0] if got else None
    if key == "blocked_by":
        return _keys(c, p, raw, "blocked_by")
    if key == "links":
        try:
            doc = canon.loads_strict(raw.encode())
        except (canon.JcsError, ValueError):
            raise OrchError(
                "parse.json" if "parse.json" in c.declared else "invalid.input", "links is a JSON object"
            ) from None
        if not isinstance(doc, dict) or set(doc) - {"repos", "branches", "prs", "external"}:
            raise OrchError("invalid.input", "links is an object with repos, branches, prs and external")
        return {**thaw(p.view.fields["links"]), **doc}
    raise OrchError("invalid.input", f"{key} cannot be set here")


def set_fields(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    sets: dict[str, Any] = {}
    for pair in args["pairs"]:
        key, _, raw = pair.partition("=")
        if f"ticket.{key}" in sets:
            raise OrchError("invalid.input", f"{key} is given twice")
        sets[f"ticket.{key}"] = parse_pair(c, p, key, raw)
    p.add({"type": "ticket.updated", "base_rev": {path: p.base(path) for path in sets}, "set": sets})
    return Out({"fields": [k[len("ticket.") :] for k in sets]})


def _acceptance(p: Projection) -> list[dict[str, str]]:
    return [dict(thaw(a)) for a in p.last_view.fields["acceptance"]]


def ac_add(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.text(args["text"], one_line=False, what="criterion")
    items = _acceptance(p)
    aid = _next_id("AC", [a["id"] for a in items])
    p.add(
        {
            "type": "ticket.updated",
            "base_rev": {"ticket.acceptance": p.base("ticket.acceptance")},
            "set": {"ticket.acceptance": [*items, {"id": aid, "text": text}]},
        }
    )
    return Out({"ac": aid})


def ac_edit(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.text(args["text"], what="criterion")
    items = _acceptance(p)
    if args["ac"] not in {a["id"] for a in items}:
        raise OrchError("not_found", f"{p.view.key} has no criterion {args['ac']}")
    items = [{**a, "text": text} if a["id"] == args["ac"] else a for a in items]
    p.add(
        {
            "type": "ticket.updated",
            "base_rev": {"ticket.acceptance": p.base("ticket.acceptance")},
            "set": {"ticket.acceptance": items},
        }
    )
    return Out({"ac": args["ac"]})


def task_add(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.text(args["text"], what="task")
    tasks = [dict(thaw(t)) for t in p.last_view.fields["tasks"]]
    proves = sorted(set(args.get("proves") or []), key=lambda x: int(x[2:]))
    known = {a["id"] for a in p.last_view.fields["acceptance"]}
    for ac in proves:
        if ac not in known:
            raise OrchError("not_found", f"{p.view.key} has no criterion {ac}")
    tid = _next_id("T", [t["id"] for t in tasks])
    verify = {"cmd": c.text(args["verify"], one_line=True, what="verify command")} if args.get("verify") else None
    task: dict[str, Any] = {"id": tid, "text": text, "verify": verify, "proves": proves}
    if args.get("assignee"):
        task["assignee"] = args["assignee"]
    p.add(
        {
            "type": "ticket.updated",
            "base_rev": {"ticket.tasks": p.base("ticket.tasks")},
            "set": {"ticket.tasks": [*tasks, task]},
        }
    )
    return Out({"task": tid})


# ----------------------------------------------------------------------------------------------------- tasks


def _task(p: Projection, tid: str) -> Any:
    t = next((t for t in p.last_view.tasks if t.id == tid), None)
    if t is None:
        raise unknown_task(p.view, tid)
    return t


def task_state(c: Call, p: Projection, args: dict[str, Any], event: str) -> Out:
    """``task.started``, ``task.skipped``, ``task.blocked`` and ``task.reopened`` (``task.done`` has its own)."""
    tid = args["task"].rpartition("/")[2]
    t = _task(p, tid)
    if event == "task.started" and t.state == "started" and t.leased_by not in (None, c.ctx.session):
        raise OrchError("lease.held", f"{tid} is leased by {t.leased_by}")
    ev: dict[str, Any] = {"type": event, "task": tid}
    if event in ("task.skipped", "task.blocked"):
        ev["reason"] = c.text(args["reason"], one_line=True, what="reason")
    elif args.get("reason"):
        ev["reason"] = c.text(args["reason"], one_line=True, what="reason")
    p.add(ev)
    return Out({"task": tid})


def kind_of(name: str) -> str:
    return _KIND.get(os.path.splitext(name)[1].lower(), "other")


def artifact_event(c: Call, name: str, data: bytes, kind: str, **extra: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not ARTIFACT_NAME.fullmatch(name):
        raise OrchError("invalid.input", f"{short(name, 40)!r} is not an artifact name (letters, digits, . _ -)")
    ev = {
        "type": "artifact.added",
        "name": name,
        "kind": kind,
        "sha256": canon.artifact_digest(data),
        "bytes": len(data),
    }
    ev.update({k: v for k, v in extra.items() if v is not None})
    return ev, {name: data}


def artifact_add(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    data = c.read_artifact(args["path"])
    name = args.get("name") or os.path.basename(args["path"])
    label = c.text(args["label"], one_line=True, what="label") if args.get("label") else None
    ev, files = artifact_event(
        c, name, data, args.get("kind", "other"), ac=args.get("ac"), task=args.get("task"), label=label
    )
    p.add(ev, artifacts=files)
    return Out({"name": name, "sha256": ev["sha256"], "bytes": len(data)}, ["orch show"])


def artifact_replace(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    name = args["name"]
    old = next((a for a in p.last_view.artifacts if a.name == name), None)
    if old is None or old.digest is None:
        raise OrchError("not_found", f"{p.view.key} has no file artifact {short(name, 40)}")
    data = c.read_artifact(args["path"])
    ev, files = artifact_event(c, name, data, old.kind, ac=old.ac, task=old.task)
    ev["type"] = "artifact.replaced"
    ev["replaces"] = old.digest
    p.add(ev, artifacts=files)
    return Out({"name": name, "sha256": ev["sha256"], "bytes": len(data)}, ["orch show"])


# ----------------------------------------------------------------------------------------------------- batches


def batch_ops() -> dict[str, tuple[str, Builder]]:
    """The operations ``apply`` accepts: registry name -> (the event type for task states or ``""``, builder)."""
    return {
        "log": ("", log),
        "ask": ("", ask),
        "section.set": ("", section_set),
        "set": ("", set_fields),
        "ac.add": ("", ac_add),
        "ac.edit": ("", ac_edit),
        "task.add": ("", task_add),
        "task.start": ("task.started", lambda c, p, a: task_state(c, p, a, "task.started")),
        "task.skip": ("task.skipped", lambda c, p, a: task_state(c, p, a, "task.skipped")),
        "task.block": ("task.blocked", lambda c, p, a: task_state(c, p, a, "task.blocked")),
        "task.reopen": ("task.reopened", lambda c, p, a: task_state(c, p, a, "task.reopened")),
        "task.done": ("task.done", lambda c, p, a: task_done_plain(c, p, a)),
        "artifact.add": ("", artifact_add),
        "artifact.replace": ("", artifact_replace),
    }


def task_done_plain(
    c: Call,
    p: Projection,
    args: dict[str, Any],
    *,
    receipt: dict[str, Any] | None = None,
    log_name: str | None = None,
    extras: tuple[tuple[dict[str, Any], dict[str, bytes]], ...] = (),
) -> Out:
    """``task.done``: the evidence artifacts first (the event may name one as its ``log``), then the task. ``--run``
    is the operation's business (it runs a command); a batch item has no ``run``."""
    tid = args["task"].rpartition("/")[2]
    _task(p, tid)
    for ev, files in extras:
        p.add(ev, artifacts=files)
    done: dict[str, Any] = {"type": "task.done", "task": tid}
    if receipt is not None:
        done["receipt"] = receipt
    if log_name is not None:
        done["log"] = log_name
    msg = args.get("message")
    if msg:
        done["text"] = c.body_text(msg, what="message")
    p.add(done)
    return Out({"task": tid})


def render_next_task(view: Any, session: str | None) -> list[str]:
    t = views.next_task(view, session)
    if t is None:
        return []
    verify = next((x["verify"] for x in view.fields["tasks"] if x["id"] == t.id), None)
    body = views.task_line(t, 200) + (f"\nverify: {short(verify['cmd'], 200)}" if verify else "")
    return fence(body, f"next task {t.id}")
