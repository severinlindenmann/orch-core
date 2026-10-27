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
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orch import canon
from orch.model.tasks import verify_cmd_problem
from orch.ops import views
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Projection, path_hash, short
from orch.ops.views import fence
from orch.schema import SECTIONS_BY_TYPE
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
    observe_repos: bool = False,
) -> Result:
    """The frame of an editing operation: lock, resolve the ticket, (check the claim), build, append, answer."""
    c = Call.of(ctx, op)
    problems: list[str] = []
    if observe_repos:  # git is read before the lock is taken: only the append of what it found holds a writer up
        c.ctx.caps.git.observe(c.store, c.resolve(args.get(ref_key), live_only=live_only).uid, problems)
        if problems and not ctx.dry_run:  # what is submitted must be what was looked at
            raise OrchError(
                "observe.unavailable",
                "; ".join(problems)[:200],
                hint="orch show shows what cannot be observed; fix settings.repos or links.branches",
            )
    with c.locked():
        view = c.resolve(args.get(ref_key), live_only=live_only)
        if claim and ctx.prepare is None:  # a person's signed edit needs no claim (F1: A types, signed by a person)
            c.require_claim(view)
        p = c.projection(view)
        out = build(c, p, args)
        done = p.commit()
        seq = done[-1].event["seq"] if done else p.last_seq
        hints = out.hints if out.hints is not None else [views.next_hint(p.last_view, ctx.session)]
        return c.result(
            p.last_view, out.data, seq=seq, hints=hints, lines=[*(f"observe: {x}" for x in problems), *out.lines]
        )


def unknown_task(view: Any, tid: str) -> OrchError:
    return OrchError("not_found", f"{view.key} has no task {tid}")


# ----------------------------------------------------------------------------------------------------- simple events


def log(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.body_text(c.read_text_source(args) or "", what="note")
    p.add({"type": "log.added", "text": text})
    return Out({}, ["orch wait" if not c.attended else views.next_hint(p.last_view, c.ctx.session)])


def _next_id(prefix: str, existing: list[str]) -> str:
    return f"{prefix}{max((int(x[len(prefix) :]) for x in existing), default=0) + 1}"


_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "ae", "Ö": "oe", "Ü": "ue"})
ASK_HINT = (
    "orch ask 'Question?' --options 'First,Second' --rec 'First'  "
    "(2 to 6 options in plain words, each up to 60 characters, comma separated)"
)


def _ask_retry(message: str) -> OrchError:
    """A mistake in the call of ``orch ask``: retryable, with the fixed command as the hint."""
    return OrchError("invalid.input", message, hint=ASK_HINT, retryable=True)


def option_slug(title: str) -> str:
    """The option key for a plain-words title: ``^[a-z][a-z0-9_]{0,31}$`` (transliterate, ``_``, ``o_`` prefix)."""
    t = title.translate(_UMLAUT).replace("ß", "ss")
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch)).lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    if not t or not t[0].isalpha():
        t = "o_" + t
    return _cut_key(t)


def _cut_key(t: str, limit: int = 32) -> str:
    if len(t) <= limit:
        return t.rstrip("_")
    cut = t[:limit]
    if "_" in cut and t[limit] != "_":
        cut = cut[: cut.rindex("_")]
    return cut.rstrip("_") or t[:limit]


def option_keys(titles: list[str]) -> list[str]:
    keys: list[str] = []
    for t in titles:
        base = option_slug(t)
        key, n = base, 1
        while key in keys:
            n += 1
            suffix = f"_{n}"
            key = _cut_key(base, 32 - len(suffix)) + suffix
        keys.append(key)
    return keys


def _resolve_rec(rec: str, titles: list[str], keys: list[str]) -> str:
    if rec in keys:
        return rec
    low = rec.strip().casefold()
    for t, k in zip(titles, keys, strict=True):
        if t.casefold() == low:
            return k
    slug = option_slug(rec)
    hits = [k for t, k in zip(titles, keys, strict=True) if option_slug(t) == slug]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise _ask_retry("--rec matches more than one option; use one of: " + ", ".join(keys))
    raise _ask_retry("--rec must be one of --options (" + ", ".join(titles) + ")")


def ask(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.text(args["text"], what="question")
    titles = [str(t).strip() for t in (args.get("options") or [])]
    if any(not t or not re.search(r"[A-Za-z0-9]", t) for t in titles):
        raise _ask_retry("an option is empty or has no letters or digits")
    keys = option_keys(titles)
    rec = args.get("rec")
    if rec is not None:
        rec = _resolve_rec(rec, titles, keys)
    to = args.get("to") or "ticket_owner"
    if to not in TO_ROLES and not _PERSON.fullmatch(to):
        raise _ask_retry("--to is a person id or one of " + ", ".join(TO_ROLES))
    qid_n = _next_id("Q", [q["id"] for q in p.last_view.fields["questions"]])
    options = [{"key": k, "label": t} for k, t in zip(keys, titles, strict=True)]
    q: dict[str, Any] = {"id": qid_n, "to": to, "text": text, "blocking": not args.get("non_blocking")}
    q["kind"] = "choice" if options else "text"  # no options: an open question (kind text)
    if args.get("why"):
        q["why"] = c.text(args["why"], what="why")
    if options:
        q["options"] = options
    if rec is not None:
        q["recommended"] = rec
    try:
        canon.validate_question(q)
    except canon.QuestionError as e:
        raise _ask_retry(str(e)) from None
    wid = c.store.state.workspace.workspace_id
    qid = canon.question_id(wid, p.uid, qid_n)
    p.add({"type": "question.asked", "question": q, "qid": qid, "hash": canon.question_hash(qid, p.uid, q)})
    return Out({"question": qid_n, "blocking": q["blocking"]}, ["orch wait" if q["blocking"] else "orch task next"])


def _widgets_addon_on(c: Call) -> bool:
    from orch.addons.install import as_addons

    try:
        a = as_addons(c.store.state.workspace.addons).get("widgets")
    except (AttributeError, KeyError, TypeError):
        return False
    return a is not None and a.enabled and not a.purged


def check_widgets(c: Call, p: Projection, sid: str, text: str) -> None:
    """Refuse a section whose ``orch`` widget blocks the strict validator (the dashboard's, ported) would not draw, so
    the author learns it where it can fix it. Text without a fence is not even parsed."""
    from orch.widgets import check

    if not check.has_fence(text):
        return
    body = c.store.body_sections(p.view.uid)
    problems = check.check_write(body, sid, text, templates_ok=_widgets_addon_on(c))
    if problems:
        raise OrchError("parse.text", check.message(problems, sid))


def section_set(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    sid = args["section"]
    if sid not in SECTIONS_BY_TYPE[p.view.type]:
        raise OrchError("invalid.input", f"a {p.view.type} ticket has no section {sid}")
    text = c.text(c.read_text_source(args, ("message", "file")) or "", limit=65536, what=f"section {sid}").strip("\n")
    check_widgets(c, p, sid, text)
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


def addon_field_value(c: Call, p: Projection, key: str, raw: str) -> Any:
    """The new value of the add-on field ``<addon>.<field>`` from the text after ``=`` (AP D5 step 1b). The add-on must
    be on (``addon.off``); the field, its type, limits and ``set_by`` are the **grant's** (the same rules replay
    applies, :func:`orch.model.addon_rules.check_field_write`). ``none`` clears. Returns the value; the caller writes it
    as ``ticket.addons.<addon>.<field>`` in an ordinary ``ticket.updated``."""
    from orch.addons.install import as_addons
    from orch.model import addon_rules
    from orch.ops import addon_gate

    name, _, fname = key.partition(".")
    addons = c.store.state.workspace.addons
    addon_gate.require_on(addons, name)
    grant = as_addons(addons)[name]
    spec = grant.fields.get(fname)
    if spec is None:
        raise OrchError("invalid.input", f"the {name} add-on declares no field {fname}", hint=f"orch describe {name}.*")
    value = _addon_value(c, spec, raw, key)
    actor = p.capture.actor if p.capture is not None else c.actor()
    if actor["kind"] == "agent":  # a person's write is judged by replay against the roles they hold on the ticket
        refusal = addon_rules.check_field_write(grant, fname, value, actor)
        if refusal is not None:
            raise OrchError("invalid.input", f"{key}: {refusal.detail}")
    return value


def _addon_value(c: Call, spec: dict[str, Any], raw: str, label: str) -> Any:
    from orch.model.addon_rules import validate_value

    kind = spec["type"]
    if _none(raw):
        return None
    if kind in ("string", "text"):
        value: Any = c.text(raw, one_line=kind == "string", what=label)
    elif kind == "integer":
        if not re.fullmatch(r"-?[0-9]{1,16}", raw):
            raise OrchError("invalid.input", f"{label} is an integer")
        value = int(raw)
    elif kind == "boolean":
        if raw not in ("true", "false"):
            raise OrchError("invalid.input", f"{label} is true or false")
        value = raw == "true"
    elif kind == "string_list":
        value = raw.split(",")
    else:  # enum, person
        value = raw
    try:
        validate_value(spec, value)
    except ValueError as e:
        raise OrchError("invalid.input", f"{label}: {e}") from None
    return value


def addon_field_base(p: Projection, path: str) -> str:
    """``base_rev`` of ``ticket.addons.<addon>.<field>``: the hash of the value now (null when unset), the one replay
    compares. Taken from the planned view under the workspace lock, so it cannot go stale between read and append."""
    name, _, fname = path[len("ticket.addons.") :].partition(".")
    current = thaw(p.last_view.fields["addons"]).get(name, {}).get(fname)
    if p.capture is not None:  # a person edits what they are shown: the current value, sent as `before`
        p.capture.before[path] = current
    return path_hash(p.last_view, path)


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
        labels = [] if _none(raw) or raw == "" else raw.split(",")  # ``labels=`` is the empty set
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
        path = f"ticket.addons.{key}" if "." in key else f"ticket.{key}"  # ``echo.points`` is an add-on field
        if path in sets:
            raise OrchError("invalid.input", f"{key} is given twice")
        sets[path] = addon_field_value(c, p, key, raw) if "." in key else parse_pair(c, p, key, raw)
    from orch.ops import addon_gate, quick

    if quick.LABEL in (sets.get("ticket.labels") or ()) and quick.LABEL not in (p.last_view.fields.get("labels") or ()):
        # a NEW quick task needs the add-on; existing ones stay
        addon_gate.require_on(c.store.state.workspace.addons, quick.ADDON)
    base = {path: addon_field_base(p, path) if path.startswith("ticket.addons.") else p.base(path) for path in sets}
    p.add({"type": "ticket.updated", "base_rev": base, "set": sets})
    return Out({"fields": [k.removeprefix("ticket.").removeprefix("addons.") for k in sets]})


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


def _verify_cmd(c: Call, raw: str) -> str:
    """A verify command, refused now when it needs a shell (it is run without one)."""
    cmd = c.text(raw, one_line=True, what="verify command")
    if problem := verify_cmd_problem(cmd):
        raise OrchError("invalid.input", f"verify command: {problem}")
    return cmd


def task_add(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    text = c.text(args["text"], what="task")
    tasks = [dict(thaw(t)) for t in p.last_view.fields["tasks"]]
    proves = sorted(set(args.get("proves") or []), key=lambda x: int(x[2:]))
    known = {a["id"] for a in p.last_view.fields["acceptance"]}
    for ac in proves:
        if ac not in known:
            raise OrchError("not_found", f"{p.view.key} has no criterion {ac}", hint="orch ac add TEXT")
    tid = _next_id("T", [t["id"] for t in tasks])
    verify = {"cmd": _verify_cmd(c, args["verify"])} if args.get("verify") else None
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


def task_edit(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    """``task.edit``: the text, the verify command and the ``proves`` list of one task. Changing the verify command or
    the criteria of a task that is done is refused: its receipt would then stand for another command or criterion
    (reopen the task first). The text of a done task may change."""
    tid = args["task"].rpartition("/")[2]
    t = _task(p, tid)
    tasks = [dict(thaw(x)) for x in p.last_view.fields["tasks"]]
    old = next(x for x in tasks if x["id"] == tid)
    new = dict(old)
    if args.get("text") is not None:
        new["text"] = c.text(args["text"], what="task")
    if args.get("verify") is not None:
        cmd = _verify_cmd(c, args["verify"]) if args["verify"] != "" else None
        new["verify"] = {"cmd": cmd} if cmd else None
    if args.get("proves") is not None:
        proves = sorted(set(args["proves"]), key=lambda x: int(x[2:]))
        known = {a["id"] for a in p.last_view.fields["acceptance"]}
        for ac in proves:
            if ac not in known:
                raise OrchError("not_found", f"{p.view.key} has no criterion {ac}", hint="orch ac add TEXT")
        new["proves"] = proves
    if new == old:
        raise OrchError("invalid.input", f"{tid} would not change")
    if t.state == "done" and (new.get("verify") != old.get("verify") or new.get("proves") != old.get("proves")):
        raise OrchError(
            "transition.refused",
            f"{tid} is done: its verify command and criteria cannot change, because its receipt stands for them; "
            "reopen it first",
            hint=f"orch task reopen {tid}",
        )
    p.add(
        {
            "type": "ticket.updated",
            "base_rev": {"ticket.tasks": p.base("ticket.tasks")},
            "set": {"ticket.tasks": [new if x["id"] == tid else x for x in tasks]},
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


#: the files one ticket may hold (design r3 part C: a no-codebase ticket files its results as artifacts)
TICKET_ARTIFACT_BUDGET = 512 << 20


def _budget(p: Projection, added: int, replaced: int = 0) -> None:
    """A ticket holds at most 512 MiB of file artifacts (replaced files count once, at their new size)."""
    held = sum(a.bytes or 0 for a in p.last_view.artifacts if a.digest is not None)
    if held - replaced + added > TICKET_ARTIFACT_BUDGET:
        raise OrchError(
            "invalid.input",
            f"{p.view.key} holds {held >> 20} MiB of files and the limit is {TICKET_ARTIFACT_BUDGET >> 20} MiB: "
            "keep large results in the work folder and add a smaller summary",
        )


def artifact_add(c: Call, p: Projection, args: dict[str, Any]) -> Out:
    if args.get("kind") == "receipt":
        raise OrchError("invalid.input", "a receipt is only made by task done --run")
    data = c.read_artifact(args["path"])
    _budget(p, len(data))
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
    if old.kind == "receipt":
        raise OrchError("invalid.input", f"{name} is a receipt: only task done --run makes one, nothing replaces it")
    data = c.read_artifact(args["path"])
    _budget(p, len(data), old.bytes or 0)
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
        "task.edit": ("", task_edit),
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
