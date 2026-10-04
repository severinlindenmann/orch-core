"""The `## Tasks` section of a ticket: format orch.tasks.v1 (docs/tasks-format.md).

    - [<box>] T<n> <text>
      - <field>: <value>

Boxes: "[ ]" todo, "[/]" doing, "[x]" done, "[-]" skipped, "[!]" blocked. Fields, at most one each
except `ref`: ref, verify, needs, owner, why, on, note, added. parse() allows some slack (blank
lines, "[X]", CRLF, any field order, a tab or 2+ spaces of indent, upper-case keys); render()
writes the canonical form and parse(render(tasks)) == tasks. Nothing else may stand in the section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from orch.errors import NotFoundError, TicketParseError, ValidationError

FORMAT = "orch.tasks.v1"
STATES = ("todo", "doing", "done", "skipped", "blocked")
OPEN_STATES = ("todo", "doing", "blocked")
BOX = {"todo": " ", "doing": "/", "done": "x", "skipped": "-", "blocked": "!"}
_STATE_OF = {" ": "todo", "/": "doing", "x": "done", "X": "done", "-": "skipped", "!": "blocked"}
FIELD_ORDER = ("ref", "verify", "needs", "owner", "why", "on", "note", "added")
OWNERS = ("agent", "human")
REF_KINDS = ("file", "static", "artifact", "ticket", "ext", "url", "section", "ac", "q")
AFTER_APPROVAL = "after plan approval"
LABEL_SEP = " — "
NEW_KEYS = ("text", "refs", "verify", "needs", "owner")

_TASK = re.compile(r"^[-*+] \[(.)\] T(\d+)(?: (.*))?$")
_FIELD = re.compile(r"^(?: {2,}|\t+)[-*+] ([A-Za-z]+):(?: (.*))?$")
_TASK_ID = re.compile(r"^T(\d+)$", re.I)
_QID = re.compile(r"^Q(\d+)$", re.I)
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")
LINES = re.compile(r"^(.*?)(?:#L(\d+)(?:-(\d+))?)?$")


class TaskParseError(TicketParseError):
    """The Tasks section breaks the grammar; `line` is 1-based within the section."""

    def __init__(self, message: str, line: int):
        super().__init__(f"Tasks line {line}: {message}", hint="fix the Tasks section in the raw file")
        self.line = line


@dataclass(frozen=True)
class Ref:
    kind: str
    target: str
    label: str | None = None

    def __str__(self) -> str:
        return f"{self.kind}:{self.target}" + (f"{LABEL_SEP}{self.label}" if self.label else "")


@dataclass
class Task:
    id: str
    state: str = "todo"
    text: str = ""
    refs: list[Ref] = field(default_factory=list)
    verify: str | None = None
    needs: list[str] = field(default_factory=list)
    owner: str = "agent"
    why: str | None = None
    on: str | None = None
    note: str | None = None
    added: str | None = None

    @property
    def num(self) -> int:
        return int(self.id[1:])

    @property
    def closed(self) -> bool:
        return self.state in ("done", "skipped")

    @property
    def added_after_approval(self) -> bool:
        return bool(self.added and self.added.endswith(AFTER_APPROVAL))


def one_line(value) -> str:
    return " ".join(str(value or "").split())


def normalize_task_id(value) -> str:
    m = _TASK_ID.match(one_line(value))
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"{value!r} is not a task id (T1, T2, …)")
    return f"T{int(m.group(1))}"


def normalize_on(value) -> str:
    v = one_line(value)
    if m := _QID.match(v):
        return f"Q{int(m.group(1))}"
    if m := _TASK_ID.match(v):
        return f"T{int(m.group(1))}"
    if _KEY.match(v):
        return v.upper()
    raise ValueError(f"on: {value!r} must be a question (Q3), a task (T4) or a ticket or external key (DEMO-0042)")


def on_kind(on: str) -> str:
    if _QID.match(on):
        return "question"
    if _TASK_ID.match(on):
        return "task"
    return "key"


def _bad_path(path: str) -> bool:
    parts = re.split(r"[/\\]", path)
    return path.startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:", path)) or ".." in parts


def parse_ref(value) -> Ref:
    raw = one_line(value)
    label = None
    if LABEL_SEP in raw:
        raw, label = raw.split(LABEL_SEP, 1)
        label = label.strip() or None
    kind, sep, target = raw.partition(":")
    kind, target = kind.strip().lower(), target.strip()
    if not sep or kind not in REF_KINDS:
        raise ValueError(f"ref {value!r} must start with one of " + ", ".join(k + ":" for k in REF_KINDS))
    if not target:
        raise ValueError(f"ref {value!r} has no target")
    if kind in ("file", "static", "artifact"):
        path = LINES.match(target).group(1)
        if _bad_path(path):
            raise ValueError(f"ref {value!r}: use a path relative to the workspace, without '..'")
        if any(c in path for c in "*?"):
            raise ValueError(f"ref {value!r}: glob patterns (* or ?) are not supported; name a file or folder")
    elif kind in ("ticket", "ext"):
        if not _KEY.match(target):
            raise ValueError(f"ref {value!r}: {kind}: takes a key like DEMO-0042")
        target = target.upper()
    elif kind == "url":
        if not target.lower().startswith(("http://", "https://")):
            raise ValueError(f"ref {value!r}: only http(s) URLs")
    elif kind == "ac":
        if not target.isdigit() or int(target) < 1:
            raise ValueError(f"ref {value!r}: ac: takes the criterion's number, e.g. ac:2")
        target = str(int(target))
    elif kind == "q":
        m = _QID.match(target)
        if not m:
            raise ValueError(f"ref {value!r}: q: takes a question id, e.g. q:Q2")
        target = f"Q{int(m.group(1))}"
    return Ref(kind, target, label)


def _set_field(t: Task, key: str, value: str, n: int) -> None:
    try:
        if key == "ref":
            t.refs.append(parse_ref(value))
        elif key == "needs":
            t.needs = [normalize_task_id(x) for x in value.split(",") if x.strip()]
        elif key == "owner":
            if value not in OWNERS:
                raise ValueError("owner: is agent or human")
            t.owner = value
        elif key == "on":
            t.on = normalize_on(value)
        elif key == "added":
            if not _STAMP.match(value.removesuffix(" " + AFTER_APPROVAL)):
                raise ValueError("added: is a stamp like 2026-10-02T09:14Z, optionally followed by 'after plan approval'")
            t.added = value
        else:  # verify, why, note
            setattr(t, key, value)
    except ValueError as e:
        raise TaskParseError(str(e), n) from None


def parse(text: str) -> list[Task]:
    tasks: list[Task] = []
    seen: set[int] = set()
    current: Task | None = None
    keys: set[str] = set()
    for n, line in enumerate((text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        line = line.rstrip()
        if not line.strip():
            continue
        m = _TASK.match(line)
        if m:
            box, num, body = m.group(1), int(m.group(2)), one_line(m.group(3))
            if box not in _STATE_OF:
                raise TaskParseError(f"unknown box [{box}] (use [ ], [/], [x], [-] or [!])", n)
            if num < 1:
                raise TaskParseError("task ids start at T1", n)
            if not body:
                raise TaskParseError(f"T{num} has no text", n)
            if num in seen:
                raise TaskParseError(f"T{num} appears twice", n)
            seen.add(num)
            current, keys = Task(id=f"T{num}", state=_STATE_OF[box], text=body), set()
            tasks.append(current)
            continue
        f = _FIELD.match(line)
        if f and current is not None:
            key, value = f.group(1).lower(), one_line(f.group(2))
            if key not in FIELD_ORDER:
                raise TaskParseError(f"unknown field {key!r} (one of {', '.join(FIELD_ORDER)})", n)
            if not value:
                raise TaskParseError(f"{key}: has no value", n)
            if key != "ref" and key in keys:
                raise TaskParseError(f"{key}: appears twice for {current.id}", n)
            keys.add(key)
            _set_field(current, key, value, n)
            continue
        raise TaskParseError("only task items (- [ ] T1 text) and their field bullets (  - ref: …) belong here", n)
    return tasks


def render(tasks: list[Task]) -> str:
    lines: list[str] = []
    for t in tasks:
        lines.append(f"- [{BOX[t.state]}] {t.id} {t.text}")
        lines += [f"  - ref: {r}" for r in t.refs]
        for key, value in (("verify", t.verify), ("needs", ", ".join(t.needs)),
                           ("owner", t.owner if t.owner != "agent" else None), ("why", t.why),
                           ("on", t.on), ("note", t.note), ("added", t.added)):
            if value:
                lines.append(f"  - {key}: {value}")
    return "\n".join(lines)


def ticket_tasks(ticket) -> list[Task]:
    return parse(ticket.section("Tasks"))


def summary(tasks: list[Task]) -> dict:
    out = {"total": len(tasks), **{s: 0 for s in STATES}}
    for t in tasks:
        out[t.state] += 1
    out["closed"] = out["done"] + out["skipped"]
    return out


def open_ids(tasks: list[Task]) -> list[str]:
    return [t.id for t in tasks if t.state in OPEN_STATES]


def doing(tasks: list[Task]) -> Task | None:
    return next((t for t in tasks if t.state == "doing"), None)


def needs_open(task: Task, tasks: list[Task]) -> list[str]:
    closed = {t.id for t in tasks if t.closed}
    return [n for n in task.needs if n not in closed]  # unknown ids count as open


def next_task(tasks: list[Task]) -> Task | None:
    return doing(tasks) or next(
        (t for t in tasks if t.state == "todo" and t.owner == "agent" and not needs_open(t, tasks)), None)


def human_ready(tasks: list[Task]) -> list[Task]:
    return [t for t in tasks if t.owner == "human" and t.state in ("todo", "doing") and not needs_open(t, tasks)]


def find(tasks: list[Task], task_id: str) -> Task:
    try:
        want = normalize_task_id(task_id)
    except ValueError as e:
        raise NotFoundError(str(e)) from None
    for t in tasks:
        if t.id == want:
            return t
    raise NotFoundError(f"no task {want}", hint="`orch task list <id>` shows the tasks")


def next_number(tasks: list[Task], used=()) -> int:
    return max({t.num for t in tasks} | set(used) | {0}) + 1


def build(raw: list, start: int) -> list[Task]:
    """New tasks from `orch task add` input (CLI flags or the YAML file), IDs from `start` on."""
    out: list[Task] = []
    for i, r in enumerate(raw, 1):
        if isinstance(r, str):
            r = {"text": r}
        if not isinstance(r, dict):
            raise ValidationError(f"task {i}: must be a mapping with a 'text'")
        unknown = sorted(set(r) - set(NEW_KEYS))
        if unknown:
            raise ValidationError(f"task {i}: unknown keys {', '.join(unknown)}", hint="keys: " + ", ".join(NEW_KEYS))
        text = one_line(r.get("text"))
        if not text:
            raise ValidationError(f"task {i}: 'text' is required")
        refs = r.get("refs") or []
        needs = r.get("needs") or []
        owner = str(r.get("owner") or "agent")
        if owner not in OWNERS:
            raise ValidationError(f"task {i}: owner is agent or human")
        try:
            out.append(Task(
                id=f"T{start + i - 1}", text=text,
                refs=[parse_ref(x) for x in ([refs] if isinstance(refs, str) else refs)],
                verify=one_line(r.get("verify")) or None,
                needs=[normalize_task_id(x) for x in (needs.split(",") if isinstance(needs, str) else needs) if str(x).strip()],
                owner=owner))
        except ValueError as e:
            raise ValidationError(f"task {i}: {e}") from None
    return out


def parse_tasks_file(text: str) -> list:
    from orch.core.model import yaml_load
    try:
        data = yaml_load(text.lstrip("\ufeff"))
    except yaml.YAMLError as e:
        raise ValidationError(f"invalid tasks file: {e}") from e
    if isinstance(data, dict):
        data = data.get("tasks")
    if not isinstance(data, list) or not data:
        raise ValidationError("tasks file must contain a non-empty 'tasks' list")
    return data


def needs_problems(tasks: list[Task]) -> tuple[list[tuple[str, str]], list[str] | None]:
    ids = {t.id for t in tasks}
    unknown = [(t.id, n) for t in tasks for n in t.needs if n not in ids]
    graph = {t.id: [n for n in t.needs if n in ids] for t in tasks}
    done: set[str] = set()

    def walk(node: str, path: list[str]) -> list[str] | None:
        for nxt in graph[node]:
            if nxt in path:
                return path[path.index(nxt):] + [nxt]
            if nxt not in done:
                found = walk(nxt, path + [nxt])
                if found:
                    return found
        done.add(node)
        return None

    for t in tasks:
        if t.id not in done:
            cycle = walk(t.id, [t.id])
            if cycle:
                return unknown, cycle
    return unknown, None


def newly_done_agent_tasks(old: list[Task], new: list[Task]) -> list[str]:
    """Agent tasks a raw edit ticks done. A task that already existed keeps its OLD owner here, so
    adding `owner: human` in the same save does not make the tick the human's (the explicit
    takeover is `orch task edit --owner human`)."""
    before = {t.id: t for t in old}
    out = []
    for t in new:
        was = before.get(t.id)
        owner = was.owner if was is not None else t.owner
        if t.state == "done" and owner == "agent" and (was is None or was.state != "done"):
            out.append(t.id)
    return out
