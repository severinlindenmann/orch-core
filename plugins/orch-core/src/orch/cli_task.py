"""`orch task …`: the ticket's task list (format orch.tasks.v1, docs/tasks-format.md)."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

task_app = typer.Typer(no_args_is_help=True,
                       help="The ticket's task list: create it right after claiming, then work it down.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output (the task-list view).")]
MessageOpt = Annotated[Optional[str], typer.Option("--message", "-m", help="Evidence, note or reason.")]
TaskArg = Annotated[str, typer.Argument(metavar="TASK", help="Task id, e.g. T3.")]
GLYPH = {"done": "✓", "doing": "◐", "todo": "○", "skipped": "–", "blocked": "▲"}


def _ctx():
    from orch import cli
    return cli, cli._ws()


def _report(ticket, as_json: bool, text: str) -> None:
    from orch.core import tasks_view
    cli, ws = _ctx()
    cli._out(tasks_view.view(ws, ticket), as_json, text)


def _ids(value: str | None) -> list[str] | None:
    return None if value is None else [p.strip() for p in value.split(",") if p.strip()]


def _ref_text(r: dict) -> str:
    kind = r["kind"]
    if kind == "file":
        text = f"{r['repo']} · {r['path']}" if r.get("repo") else r["path"]
        text += f"#L{r['lines']}" if r.get("lines") else ""
    elif kind == "ac":
        text = f'ac:{r["target"]} "{r["text"]}"' if r.get("text") else f"ac:{r['target']}"
    elif kind == "ticket":
        text = r["target"] + (f" ({r['status']})" if r.get("status") else "")
    else:
        text = f"{kind}:{r['target']}"
    text += f" — {r['label']}" if r.get("label") and kind != "url" else ""
    return text + ("" if r.get("exists", True) else "  (missing)")


def _on_text(on: dict) -> str:
    if on["kind"] == "ticket":
        return f"{on['id']} ({on['status']})"
    if on["kind"] == "question" and on.get("answered"):
        return f"{on['id']} (answered)"
    return on["id"]


def render_text(v: dict, title: str) -> str:
    s = v["summary"]
    head = (f"{v['ticket']} · {title} · {v['status']} · {s['closed']} of {s['total']} closed "
            f"({s['done']} done, {s['skipped']} skipped)")
    if v.get("error"):
        return f"{head}\n  {v['error']}"
    if not v["tasks"]:
        return f"{head}\n  no tasks yet: orch task add {v['ticket']} --file tasks.yaml"
    lines = [head]
    for t in v["tasks"]:
        tail = ""
        if t["state"] == "doing":
            tail = "← doing"
        elif t["state"] == "skipped":
            tail = f"skipped: {t['why'] or '(no reason)'}"
        elif t["state"] == "blocked":
            tail = ("blocked" + (f" on {_on_text(t['on_ref'])}" if t.get("on_ref") else "")
                    + (f": {t['why']}" if t["why"] else ""))
        elif t["state"] == "todo" and t["needs_open"]:
            tail = "needs " + ", ".join(t["needs_open"])
        if t["owner"] == "human" and t["state"] not in ("done", "skipped"):
            tail = f"human · {tail}" if tail else "human"
        lines.append(f"  {GLYPH[t['state']]} {t['id']:<4}{t['text']}" + (f"   {tail}" if tail else ""))
        if t["id"] == v["next"]:
            lines += [f"        ref     {_ref_text(r)}" for r in t["refs"]]
            if t["verify"]:
                lines.append(f"        verify  {t['verify']}")
            if t["note"]:
                lines.append(f"        note    {t['note']}")
    nxt = v["next"]
    foot = f"next: {nxt} ({'doing' if nxt == v['doing'] else 'to do'})" if nxt else "next: none"
    foot += (" · testing needs " + ", ".join(v["open"]) + " closed") if v["open"] else " · all tasks closed"
    return "\n".join(lines + [foot])


@task_app.command("add")
def add(ref: str,
        text: Annotated[Optional[str], typer.Argument(help="The task, one line.")] = None,
        file: Annotated[Optional[Path], typer.Option("--file", exists=True, dir_okay=False, help="YAML with a `tasks:` list.")] = None,
        refs: Annotated[Optional[list[str]], typer.Option("--ref", help="KIND:TARGET, repeatable (file: static: artifact: ticket: ext: url: section: ac: q:).")] = None,
        verify: Annotated[Optional[str], typer.Option("--verify", help="Command or check that proves it.")] = None,
        needs: Annotated[Optional[str], typer.Option("--needs", help="Tasks to close first, e.g. T2,T3.")] = None,
        owner: Annotated[str, typer.Option("--owner", help="agent | human")] = "agent",
        after: Annotated[Optional[str], typer.Option("--after", help="Insert below this task (position, not a dependency).")] = None,
        json_out: JsonOpt = False) -> None:
    """Add a task, or several from a YAML file. IDs continue after the highest ever used."""
    from orch.core import tasks as tk
    from orch.errors import UsageError
    cli, ws = _ctx()
    if (text is None) == (file is None):
        raise UsageError("pass the task text or --file",
                         hint='orch task add L-0042 "Write the job" · orch task add L-0042 --file tasks.yaml')
    if file is not None:
        if refs or verify or needs or owner != "agent":
            raise UsageError("with --file, put refs, verify, needs and owner into the file")
        raw = tk.parse_tasks_file(file.read_text(encoding="utf-8"))
    else:
        raw = [{"text": text, "refs": list(refs or []), "verify": verify, "needs": _ids(needs) or [], "owner": owner}]
    t, added = cli._ops(ws).task_add(ref, raw, after=after)
    _report(t, json_out, f"{t.id}: added {', '.join(added)}")


@task_app.command("edit")
def edit(ref: str, task: TaskArg,
         text: Annotated[Optional[str], typer.Option("--text", help="New text, one line.")] = None,
         refs: Annotated[Optional[list[str]], typer.Option("--ref", help="Add a ref, repeatable.")] = None,
         drop_refs: Annotated[Optional[list[str]], typer.Option("--drop-ref", help="Remove a ref (KIND:TARGET), repeatable.")] = None,
         verify: Annotated[Optional[str], typer.Option("--verify")] = None,
         no_verify: Annotated[bool, typer.Option("--no-verify", help="Remove the verify line.")] = False,
         needs: Annotated[Optional[str], typer.Option("--needs", help="Replace the needs, e.g. T2,T3.")] = None,
         no_needs: Annotated[bool, typer.Option("--no-needs", help="Remove the needs.")] = False,
         owner: Annotated[Optional[str], typer.Option("--owner", help="agent | human (the human only)")] = None,
         json_out: JsonOpt = False) -> None:
    """Change an open task's text, refs, verify line, needs or owner."""
    cli, ws = _ctx()
    t = cli._ops(ws).task_edit(ref, task, text=text, add_refs=list(refs or []), drop_refs=list(drop_refs or []),
                               verify=verify, clear_verify=no_verify, needs=_ids(needs), clear_needs=no_needs,
                               owner=owner)
    _report(t, json_out, f"{t.id}: edited {task.upper()}")


@task_app.command("start")
def start(ref: str, task: TaskArg, json_out: JsonOpt = False) -> None:
    """Start a task (only one is in progress; its needs must be closed)."""
    cli, ws = _ctx()
    t = cli._ops(ws).task_start(ref, task)
    _report(t, json_out, f"{t.id}: started {task.upper()}")


@task_app.command("done")
def done(ref: str, task: TaskArg, message: MessageOpt = None,
         run: Annotated[bool, typer.Option("--run", help="Run the verify line here (a command, or check:<name> from "
                                                         "the workspace config), keep a receipt and draw it in "
                                                         "Verification; ticks the task only when every step passed.")] = False,
         timeout: Annotated[int, typer.Option("--timeout", min=1, help="Seconds before --run gives up.")] = 1800,
         json_out: JsonOpt = False) -> None:
    """Tick a task; -m says what proved it (required when it has a verify line), or --run proves it with a receipt."""
    cli, ws = _ctx()
    if run:
        from orch.core import store
        rec = cli._ops(ws).task_done_run(ref, task, cwd=Path.cwd(), timeout=timeout, note=message)
        t = store.load(ws, ref)[1]
        _report(t, json_out, f"{t.id}: {task.upper()} done · receipt {rec['receipt']}")
        return
    t = cli._ops(ws).task_done(ref, task, message)
    _report(t, json_out, f"{t.id}: {task.upper()} done")


@task_app.command("skip")
def skip(ref: str, task: TaskArg, message: MessageOpt = None, json_out: JsonOpt = False) -> None:
    """Close a task without doing it; -m gives the reason."""
    cli, ws = _ctx()
    t = cli._ops(ws).task_skip(ref, task, message or "")
    _report(t, json_out, f"{t.id}: skipped {task.upper()}")


@task_app.command("block")
def block(ref: str, task: TaskArg, message: MessageOpt = None,
          on: Annotated[Optional[str], typer.Option("--on", help="What it waits for: Q3, T4, DEMO-0042, ABC-77.")] = None,
          json_out: JsonOpt = False) -> None:
    """Mark a task blocked; -m gives the reason."""
    cli, ws = _ctx()
    t = cli._ops(ws).task_block(ref, task, message or "", on=on)
    _report(t, json_out, f"{t.id}: blocked {task.upper()}")


@task_app.command("reopen")
def reopen(ref: str, task: TaskArg, message: MessageOpt = None, json_out: JsonOpt = False) -> None:
    """Put a done, skipped or blocked task back to to-do."""
    cli, ws = _ctx()
    t = cli._ops(ws).task_reopen(ref, task, message)
    _report(t, json_out, f"{t.id}: reopened {task.upper()}")


@task_app.command("list")
def list_(ref: str, json_out: JsonOpt = False) -> None:
    """Where the work stands: the doing task, `next`, its refs and verify line, what blocks testing."""
    from orch.core import store, tasks_view
    cli, ws = _ctx()
    _, t = store.load(ws, ref)
    v = tasks_view.view(ws, t)
    cli._out(v, json_out, render_text(v, t.title))
    if v["error"]:
        raise typer.Exit(6)  # a broken Tasks section is a broken ticket file (exit 6); the view keeps the error
