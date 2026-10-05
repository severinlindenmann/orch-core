from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from orch import __version__
from orch.errors import OrchError, UsageError

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False,
                  help="Local ticket system with human-in-the-loop gates.")
section_app = typer.Typer(no_args_is_help=True, help="Edit ticket sections.")
artifact_app = typer.Typer(no_args_is_help=True, help="Files produced for a ticket.")
app.add_typer(section_app, name="section")
app.add_typer(artifact_app, name="artifact")
from orch.cli_task import task_app  # noqa: E402  (cli_task imports orch.cli lazily, inside its commands)
app.add_typer(task_app, name="task")

from orch.cli_addon import addon_app  # noqa: E402  (light: commands import their own modules)
app.add_typer(addon_app, name="addon")
from orch.cli_widget import widget_app  # noqa: E402  (light: commands import their own modules)
app.add_typer(widget_app, name="widget")
from orch.cli_epic import epic_app, sprint_app  # noqa: E402  (light: commands import their own modules)
app.add_typer(epic_app, name="epic")
app.add_typer(sprint_app, name="sprint")
from orch.cli_permit import dark_app, factory_app, permit_app  # noqa: E402  (light: commands import their own modules)
app.add_typer(permit_app, name="permit")
app.add_typer(dark_app, name="dark")
app.add_typer(factory_app, name="factory")
ledger_app = typer.Typer(no_args_is_help=True, help="The approval ledger on this machine (human only).")
app.add_typer(ledger_app, name="ledger")
schema_app = typer.Typer(no_args_is_help=True, help="The ticket model as JSON, for tools such as phone apps.")
app.add_typer(schema_app, name="schema")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
MessageOpt = Annotated[Optional[str], typer.Option("--message", "-m", help="Text given inline.")]
FileOpt = Annotated[Optional[Path], typer.Option("--file", exists=True, dir_okay=False, help="Read the text from a file.")]
ADDON_COMMANDS = frozenset({"serve", "addon"})  # the only commands that may import addon code (spec A1 §5.2)
_workspace = None  # one Workspace (and one addon load) per run() call


# -- helpers ---------------------------------------------------------------------------

def _ws():
    global _workspace
    if _workspace is None:
        from orch.core.workspace import Workspace
        _workspace = Workspace.open()
    return _workspace


def _ops(ws, actor=None):
    from orch.actor import cli_actor
    from orch.core.ops import Ops
    return Ops(ws, actor or cli_actor())


def _read(file: Path, ws=None) -> str:
    """A file the command was handed, read as text. An agent may hand only a file inside the workspace (no symbolic
    link on the way, not in orch's config dir): fsutil.agent_source."""
    from orch.actor import cli_actor
    from orch.core.fsutil import agent_source
    f = agent_source(ws or _ws(), cli_actor(), file)
    if f is None:
        return file.read_text(encoding="utf-8")
    with f:  # a factory session: only the descriptor agent_source opened and checked
        return f.read().decode("utf-8")


def _text(message: str | None, file: Path | None) -> str:
    if (message is None) == (file is None):
        raise UsageError("pass exactly one of -m/--message or --file")
    return message if message is not None else _read(file)


def _out(data, as_json: bool, text: str) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str) if as_json else text)


def _view(ws, ticket) -> dict:
    from orch.core import query, store
    return query.ticket_view(ws, store.resolve(ws, ticket.id).path, ticket)


def _row(e) -> dict:
    m = e.meta or {}
    return {
        "id": e.id, "status": e.status, "type": m.get("type"), "priority": m.get("priority"),
        "size": m.get("size"), "title": m.get("title"),
        "external": [x.get("key") for x in m.get("external") or [] if isinstance(x, dict)],
        "error": e.error,
    }


def _fmt(r: dict) -> str:
    title = r["title"] if r["title"] is not None else f"⚠ {r['error']}"
    ext = f"  [{', '.join(r['external'])}]" if r["external"] else ""
    return f"{r['id']:<8} {r['status']:<12} {r['type'] or '':<13} {r['size'] or '':<2}  {title}{ext}"


# -- entry point -----------------------------------------------------------------------

def _report(e: OrchError, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps({"error": type(e).__name__, "message": e.message, "hint": e.hint, "exit": e.exit_code}))
        return
    from orch.textsafe import visible
    typer.echo(f"error: {visible(e.message)}", err=True)  # messages may quote ticket text
    if e.hint:
        typer.echo(f"hint: {visible(e.hint)}", err=True)


def run(argv: list[str] | None = None) -> int:
    # typer >= 0.16 vendors its own click fork (typer._click) and raises exceptions from it,
    # but a real top-level `click` package may also be importable (not a declared runtime
    # dependency of this package, but pulled in transitively by e.g. the optional `dashboard`
    # extra via uvicorn). The two are different classes, so we must catch whichever ones are
    # actually importable, not just one or the other.
    click_exceptions: list[type[BaseException]] = []
    for modpath in ("click.exceptions", "typer._click.exceptions"):
        try:
            module = __import__(modpath, fromlist=["ClickException"])
        except ImportError:
            continue
        click_exceptions.append(module.ClickException)
    click_exceptions_t = tuple(click_exceptions)

    global _workspace
    argv = list(sys.argv[1:] if argv is None else argv)
    _workspace = None  # never reuse a workspace from an earlier call (tests run many workspaces per process)
    from orch.addons.loader import no_addon_imports
    first = argv[0] if argv else ""
    try:
        with contextlib.nullcontext() if first in ADDON_COMMANDS else no_addon_imports():
            rv = app(args=argv, prog_name="orch", standalone_mode=False)
        return rv if isinstance(rv, int) else 0
    except OrchError as e:
        _report(e, "--json" in argv)
        return e.exit_code
    except typer.Exit as e:
        return e.exit_code
    except typer.Abort:
        typer.echo("aborted", err=True)
        return 1
    except click_exceptions_t as e:
        e.show()
        return 2
    except KeyboardInterrupt:
        return 130


def _utf8_streams() -> None:
    # Windows pipes default to the ANSI code page (cp1252), which cannot encode "→" in rules and Log lines.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _utf8_streams()
    sys.exit(run())


# -- create / read ---------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the orch version."""
    typer.echo(__version__)


@app.command()
def new(
    title: Annotated[str, typer.Option("--title", "-t", help="Ticket title.")],
    type_: Annotated[str, typer.Option("--type", help="feature|bug|chore|spike|investigation|epic")] = "feature",
    priority: Annotated[str, typer.Option("--priority", help="low|normal|high|urgent")] = "normal",
    size: Annotated[str, typer.Option("--size", help="xs|s|m|l (xs skips the plan gate)")] = "m",
    from_: Annotated[Optional[str], typer.Option("--from", help="Create as follow-up of this ticket.")] = None,
    external: Annotated[Optional[str], typer.Option("--external", help="External key, e.g. ABC-123.")] = None,
    epic: Annotated[Optional[str], typer.Option("--epic", help="Create as a child of this epic.")] = None,
    sprint: Annotated[Optional[str], typer.Option("--sprint", help="A sprint id from the workspace config.")] = None,
    body_file: Annotated[Optional[Path], typer.Option(
        "--body-file", exists=True, dir_okay=False,
        help="Markdown for the Ask. Its `## Requirements`, `## Acceptance criteria`, `## Out of scope` and "
             "`## Summary` parts (also `###`) go into those sections.")] = None,
    requirements_file: Annotated[Optional[Path], typer.Option(
        "--requirements-file", exists=True, dir_okay=False, help="Markdown for the Requirements section.")] = None,
    acceptance_file: Annotated[Optional[Path], typer.Option(
        "--acceptance-file", exists=True, dir_okay=False,
        help="Markdown for the Acceptance criteria section (`- [ ]` lines).")] = None,
    out_of_scope_file: Annotated[Optional[Path], typer.Option(
        "--out-of-scope-file", exists=True, dir_okay=False, help="Markdown for the Out of scope section.")] = None,
    summary_file: Annotated[Optional[Path], typer.Option(
        "--summary-file", exists=True, dir_okay=False, help="Markdown for the Summary section.")] = None,
    json_out: JsonOpt = False,
) -> None:
    """Create a ticket in backlog.

    The requirements gate refuses to approve while Requirements or Acceptance criteria are empty: give them here
    (headings in --body-file, or their own files) or later with `orch section set <id> Requirements --file …`.
    The Ask is the request in the requester's words."""
    from orch.core.body import split_body
    ws = _ws()
    ask, sections = split_body(_read(body_file, ws)) if body_file else ("", {})
    for name, f in (("Requirements", requirements_file), ("Acceptance criteria", acceptance_file),
                    ("Out of scope", out_of_scope_file), ("Summary", summary_file)):
        if f is None:
            continue
        if name in sections:
            raise UsageError(f"{name} is given twice: in --body-file and in its own file", hint="keep one")
        sections[name] = _read(f, ws)
    ops = _ops(ws)
    t = ops.new(title, type=type_, priority=priority, size=size, ask=ask, external=external, from_ref=from_,
                epic=epic, sprint=sprint, sections=sections)
    _warn(ops)
    _out({**_view(ws, t), "warnings": ops.warnings} if ops.warnings else _view(ws, t), json_out,
         f"created {t.id} in backlog: {t.title}")


def _warn(ops) -> None:
    for warning in ops.warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command("list")
def list_(
    status: Annotated[Optional[str], typer.Option("--status")] = None,
    label: Annotated[Optional[str], typer.Option("--label")] = None,
    mine: Annotated[bool, typer.Option("--mine", help="Only tickets claimed by this session.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """List tickets."""
    from orch.actor import session_id
    from orch.core import query
    ws = _ws()
    rows = [_row(e) for e in query.list_tickets(ws, status=status, label=label, session=session_id() if mine else None)]
    if json_out:  # whose move it is, by the dashboard's own rules (data.cards)
        from orch.dashboard.data.cards import ticket_moves
        moves = ticket_moves(ws)
        rows = [{**r, "move": moves.get(r["id"])} for r in rows]
    _out(rows, json_out, "\n".join(_fmt(r) for r in rows) or "no tickets")


@app.command()
def show(ref: str, json_out: JsonOpt = False,
         widgets: Annotated[bool, typer.Option("--widgets", help="Each widget's text alternative after its section.")] = False,
         section: Annotated[str | None, typer.Option("--section", metavar="NAME",
                                                     help="Only the ticket's status and this section.")] = None,
         lines: Annotated[int | None, typer.Option("--lines", min=1, metavar="N",
                                                   help="Only the first N lines.")] = None) -> None:
    """Show a ticket (by ID, number or external key). --section and --lines limit the output, so no pipe into head
    or grep is needed."""
    from orch.core import store
    if json_out and (section is not None or lines is not None):
        raise UsageError("--section and --lines limit the text output; --json gives the whole ticket",
                         hint="use one of them: --json, or --section/--lines")
    ws = _ws()
    path, t = store.load(ws, ref)
    raw = path.read_text(encoding="utf-8")
    if section is not None:
        name = next((s for s in t.sections if s.casefold() == section.strip().casefold()), None)
        if name is None:
            raise UsageError(f"{t.id} has no section {section!r}", hint="sections: " + ", ".join(t.sections))
        text = f"{t.id} · status: {t.status}\n\n## {name}\n{t.section(name)}"
        typer.echo("\n".join(text.splitlines()[:lines]) if lines else text)
        return
    blocks = ctx = None
    if widgets:
        from orch.widgets import Ctx, check_ticket
        blocks, ctx = check_ticket(t, raw=raw, ws=ws), Ctx.of(ws, t)
    if json_out:
        from orch.dashboard.data.cards import move_summary, ticket_card
        extra = {}
        if widgets:
            from orch.widgets import render_text
            extra["widgets"] = [{**b.to_dict(), "text": render_text(b, ctx)} for b in blocks]
        _out({**_view(ws, t), "move": move_summary(ticket_card(ws, t)), **extra}, True, "")
    else:
        if widgets:
            from orch.widgets.render import annotate
            raw = annotate(raw, blocks, ctx)
        text = f"{path.relative_to(ws.home).as_posix()}\n\n{raw}"
        typer.echo("\n".join(text.splitlines()[:lines]) if lines else text)


@app.command()
def search(text: str, json_out: JsonOpt = False) -> None:
    """Full-text search over ticket files."""
    from orch.core import query
    rows = [_row(e) for e in query.search(_ws(), text)]
    _out(rows, json_out, "\n".join(_fmt(r) for r in rows) or "no matches")


@app.command()
def path(ref: str) -> None:
    """Print the absolute path of a ticket file."""
    from orch.core import store
    typer.echo(str(store.resolve(_ws(), ref).path))


@app.command("next")
def next_(json_out: JsonOpt = False) -> None:
    """Open, unblocked tickets by priority (top 5)."""
    from orch.core import query
    rows = [_row(e) for e in query.next_tickets(_ws())][:5]
    _out(rows, json_out, "\n".join(_fmt(r) for r in rows) or "nothing open")


# -- work ------------------------------------------------------------------------------

@app.command()
def claim(ref: str, json_out: JsonOpt = False) -> None:
    """Claim a ticket for this session (open → in-progress)."""
    ws = _ws()
    t = _ops(ws).claim(ref)
    _out(_view(ws, t), json_out, f"claimed {t.id} ({t.status})")


@app.command()
def release(ref: str, json_out: JsonOpt = False) -> None:
    """Release this session's claim."""
    ws = _ws()
    t = _ops(ws).release(ref)
    _out(_view(ws, t), json_out, f"released {t.id}")


@app.command()
def log(ref: str, message: Annotated[str, typer.Option("--message", "-m")], json_out: JsonOpt = False) -> None:
    """Append a line to the ticket Log."""
    ws = _ws()
    t = _ops(ws).log(ref, message)
    _out(_view(ws, t), json_out, f"{t.id}: logged")


@app.command()
def wait(ref: str,
         timeout: Annotated[float, typer.Option("--timeout", help="Give up after this many seconds (0 = never).")] = 0.0,
         after: Annotated[Optional[str], typer.Option("--after", help="Event seq or cursor to start after (default: your last event on the ticket).")] = None,
         json_out: JsonOpt = False) -> None:
    """Wait until the human answers, approves, requests changes or gives a verdict on a ticket. Agents may run it."""
    from dataclasses import asdict

    from orch.core import store
    from orch.core.wait import wait_for_human
    from orch.errors import WaitTimeout

    ws = _ws()
    from orch.errors import UsageError
    try:
        event = wait_for_human(ws, ref, after=after, timeout=timeout)
    except ValueError:
        raise UsageError("--after takes an event number or the cursor a previous wait printed") from None
    if event is None:
        raise WaitTimeout(f"no human decision on {store.resolve(ws, ref).id} within {timeout:g} s",
                          hint="run orch wait again, or stop and tell the user what you are waiting for")
    status = store.resolve(ws, event.ticket).status
    who = "by the factory's state" if event.kind.startswith("factory.") else "by the human"
    _out({"ticket": event.ticket, "event": asdict(event), "status": status,
          "cursor": event.data.get("cursor", event.seq) if event.kind.startswith("factory.") else event.seq}, json_out,
         f"{event.ticket}: {event.kind} {who} (status {status})")


@app.command()
def state(ref: str, message: MessageOpt = None, file: FileOpt = None, json_out: JsonOpt = False) -> None:
    """Rewrite the Current state section (session handoff)."""
    ws = _ws()
    t = _ops(ws).set_state(ref, _text(message, file))
    _out(_view(ws, t), json_out, f"{t.id}: current state updated")


@section_app.command("set")
def section_set(ref: str, name: str, message: MessageOpt = None, file: FileOpt = None, json_out: JsonOpt = False) -> None:
    """Replace one section, e.g. `orch section set L-0042 Plan --file plan.md`."""
    ws = _ws()
    ops = _ops(ws)
    t = ops.set_section(ref, name, _text(message, file))
    _warn(ops)
    _out({**_view(ws, t), "warnings": ops.warnings} if ops.warnings else _view(ws, t), json_out,
         f"{t.id}: {name} updated")


@app.command()
def link(
    ref: str,
    repo: Annotated[Optional[str], typer.Option("--repo")] = None,
    pr: Annotated[Optional[str], typer.Option("--pr", help="PR/MR URL, or its number in --repo (default: the workspace repo).")] = None,
    branch: Annotated[Optional[str], typer.Option("--branch")] = None,
    worktree: Annotated[Optional[str], typer.Option("--worktree")] = None,
    external: Annotated[Optional[str], typer.Option("--external")] = None,
    epic: Annotated[Optional[str], typer.Option("--epic", help="Put the ticket into this epic.")] = None,
    no_epic: Annotated[bool, typer.Option("--no-epic", help="Take the ticket out of its epic.")] = False,
    sprint: Annotated[Optional[str], typer.Option("--sprint", help="Plan the ticket into this sprint.")] = None,
    no_sprint: Annotated[bool, typer.Option("--no-sprint", help="Take the ticket out of its sprint.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Link a PR/MR, branch, worktree or external key; put a ticket into an epic or a sprint."""
    ws = _ws()
    t = _ops(ws).link(ref, repo=repo, pr=pr, branch=branch, worktree=worktree, external=external, epic=epic,
                      no_epic=no_epic, sprint=sprint, no_sprint=no_sprint)
    _out(_view(ws, t), json_out, f"{t.id}: linked")


# -- questions and gates (human-only where noted) --------------------------------------

def _human(ws, ref: str):
    from orch.actor import human_actor
    from orch.core import store
    return human_actor(store.resolve(ws, ref).id)


@app.command()
def ask(
    ref: str,
    file: Annotated[Path, typer.Option("--file", exists=True, dir_okay=False, help="YAML with a `questions:` list.")],
    json_out: JsonOpt = False,
) -> None:
    """Ask questions with options and a recommended default; blocking ones move in-progress → waiting."""
    from orch.core.questions import parse_ask_file
    ws = _ws()
    t, added = _ops(ws).ask(ref, parse_ask_file(_read(file, ws)))
    _out({"ticket": t.id, "status": t.status, "questions": added}, json_out,
         f"{t.id}: asked {', '.join(q['id'] for q in added)} (status {t.status})")


DryRunOpt = Annotated[bool, typer.Option(
    "--dry-run", help="Check everything and print what would happen; asks nothing and writes nothing.")]


def _short(h: str | None) -> str:
    body = str(h or "").removeprefix("sha256:")
    return f"sha256 {body[:8]}…" if body else "no hash"


def _human_op(ws, ref: str, call, describe, *, dry_run: bool, json_out: bool, bind=None, bind_file: bool = False,
              bound: dict | None = None):
    """A human-only command (#7). Order: refuse inside an agent harness or without a terminal, then run the very same
    Ops method as a dry run (every check, nothing written), then show what it binds and ask for the typed id, and only
    then apply it, bound to what the dry run saw: `bind(preview)` gives the keyword arguments (an expected hash) so
    text an agent edits while the human types is refused; `bind_file` (verdict, move: no gate hash) binds the ticket
    file as it was when the preview read it. The preview runs outside the ticket lock; the real write repeats every
    check inside it. `--dry-run` writes nothing and asks nothing, so it needs no terminal; inside an agent harness it
    is refused like the real command (the Ops method checks the harness before the preview runs).
    `bound`: the keyword arguments (the hash of what is shown, read before the preview) for the preview and the real
    call alike; Ops requires that hash for every human decision.
    Returns the applied ticket, or the preview in a dry run."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import store
    from orch.core.events import Actor
    from orch.core.ops import Ops, file_stamp
    if not dry_run:
        require_human_terminal("human-only action")
    stamp = file_stamp(store.resolve(ws, ref).path)
    preview = call(Ops(ws, Actor("human", "you", "tty"), dry_run=True), dict(bound or {}))
    if dry_run:
        return preview
    typer.echo(describe(preview), err=json_out)
    actor = confirm_typed(preview.id)
    ops = _ops(ws, actor)
    if bind_file:
        ops.expected_stamp = stamp
    return call(ops, dict(bound) if bound is not None else bind(preview) if bind else {})


def _gated_text(t, gate: str, verb: str = "Approving") -> list[str]:
    """Exactly what an approval of `gate` binds (orch.core.gates.normalized_text: the sections and size/type the hash
    covers), with hidden characters escaped, as `orch ledger adopt` shows it."""
    from orch.core.gates import normalized_text
    from orch.textsafe import lines, visible
    return [f"{verb} the {gate} of {t.id}: {visible(t.title)}"] + [f"  | {x}" for x in lines(normalized_text(t, gate))]


def _question_text(q: dict) -> list[str]:
    """The question an answer binds (text, why, options as hashed by question_hash) and the answer chosen."""
    from orch.textsafe import lines, visible
    out = [f"{visible(q.get('id'))}: {visible(q.get('text'))}"]
    if q.get("why"):
        out += [f"  why: {line}" for line in lines(q.get("why"))]
    for o in q.get("options") or []:
        if isinstance(o, dict):
            out.append(f"  {visible(o.get('key'))}) {visible(o.get('label'))}"
                       + (f" · cost: {visible(o.get('cost'))}" if o.get("cost") else ""))
    shown = ", ".join(q["answer"]) if isinstance(q.get("answer"), list) else q.get("answer")
    out.append(f"  answer: {visible(shown)}" + (f" — note: {visible(q.get('note'))}" if q.get("note") else ""))
    return out


def _dry(ws, t, json_out: bool, text: str) -> None:
    _out({**_view(ws, t), "dry_run": True}, json_out, f"dry run: {text}; nothing was written")


@app.command()
def answer(ref: str, qid: str, value: str,
           note: Annotated[Optional[str], typer.Option("--note")] = None, dry_run: DryRunOpt = False,
           json_out: JsonOpt = False) -> None:
    """Answer a question. Human only."""
    from orch.core import store
    from orch.core.questions import find_question, question_hash
    ws = _ws()
    qh = question_hash(find_question(store.load(ws, store.resolve(ws, ref).id)[1], qid))
    t = _human_op(ws, ref, lambda ops, kw: ops.answer(ref, qid, value, note=note, **kw),
                  lambda p: "\n".join(_question_text(find_question(p, qid))
                                      + [f"{p.id}: answer {qid.upper()} as shown ({_short(qh)});"
                                         f" status then {p.status}"]),
                  bound={"expected_hash": qh}, dry_run=dry_run, json_out=json_out)
    if dry_run:
        return _dry(ws, t, json_out, f"{t.id}: would answer {qid.upper()} (status then {t.status})")
    _out(_view(ws, t), json_out, f"{t.id}: answered {qid.upper()} (status {t.status})")


_ALL = "\x00all"  # --despite-open-question given with `plans` (no keys)


def _keys(ws, raw: str | None) -> list[str]:
    from orch.core.ids import normalize_ref
    return [normalize_ref(ws, k.strip()).upper() for k in (raw or "").split(",") if k.strip()]


@app.command()
def approve(ref: str, gate: Annotated[str, typer.Argument(help="requirements | plan | plans (an epic's children)")],
            despite_open_question: Annotated[bool, typer.Option(
                "--despite-open-question",
                help="Approve although a line reads as an open question for you (you read it; it is not one). "
                     "With `plans` only when exactly one plan has such a line.")] = False,
            despite_on: Annotated[Optional[str], typer.Option(
                "--despite-open-question-on", metavar="KEY,...",
                help="With `plans`: the children whose open-question line you read and waive, one by one.")] = None,
            only: Annotated[Optional[str], typer.Option(
                "--only", metavar="KEY,...", help="With `plans`: approve only these children's plans.")] = None,
            delegate: Annotated[bool, typer.Option(
                "--delegate", help="Epics: let agents auto-approve children they add, within the limits.")] = False,
            max_children: Annotated[Optional[int], typer.Option(
                "--max-children", help="With --delegate: at most this many auto-approved children (default 10).")] = None,
            max_size: Annotated[Optional[str], typer.Option(
                "--max-size", help="With --delegate: the largest size auto-approved (default m, up to l).")] = None,
            factory: Annotated[bool, typer.Option(
                "--factory", help="Epics: start it as an AI Factory (implies --delegate; 25 children or 72 hours "
                                  "by default, children up to size m). Needs factory.enabled.")] = False,
            dark: Annotated[bool, typer.Option(
                "--dark", help="Epics: start it as a Dark AI Factory (implies --factory): its shell commands run "
                               "from the Dark profile without asking you. Needs `orch factory dark on`.")] = False,
            release: Annotated[Optional[str], typer.Option(
                "--release", metavar="merge|dev|prod",
                help="With --dark: release up to this stage by itself, using the release recipe on this machine "
                     "(`orch factory release set`). prod releases to production, never before the recipe's release "
                     "window opens.")] = None,
            rollback: Annotated[bool, typer.Option(
                "--rollback", help="With --release prod: run the recipe's rollback by itself when the production "
                                   "check fails (nothing else).")] = False,
            close: Annotated[bool, typer.Option(
                "--close", help="With --dark: close the epic by itself when everything is proven. This replaces "
                                "your verdict for this run; Reopen stays yours.")] = False,
            dry_run: DryRunOpt = False, json_out: JsonOpt = False) -> None:
    """Approve the requirements or plan gate. Human only.

    On an epic this approves its charter: the epic's requirements and every child that is not done (in any status),
    its requirements and its plan, all printed before the typed confirmation; the approval binds exactly what was
    printed. `orch approve <epic> plans` approves only the children's plans that wait for approval (in progress or
    waiting), each printed with its hash, after one typed confirmation of the epic's key."""
    from orch.core import epics, store
    ws = _ws()
    if dark:
        from orch.core.permits import dark_on
        if not dark_on(ws):
            raise UsageError("Dark AI Factory is switched off in this checkout",
                             hint="set factory.enabled to true in orchestrator/config.json and run `orch factory dark "
                                  "on` in your own terminal (docs/factory.md)")
    if release is not None and release != "none" and not dark:
        raise UsageError("--release goes with --dark: only a Dark charter signs a release")
    if rollback and release != "prod":
        raise UsageError("--rollback goes with --release prod")
    if close and not dark:
        raise UsageError("--close goes with --dark: only a Dark charter closes the epic by itself")
    factory = factory or dark
    delegate = delegate or factory
    if (max_children is not None or max_size is not None) and not delegate:
        raise UsageError("--max-children and --max-size go with --delegate")
    limits = {"max_children": max_children, "max_size": max_size} if delegate else None
    if factory:
        limits["factory"] = True
    if dark:
        limits["dark"] = True
        if release is not None:
            limits["release"] = release
        if rollback:
            limits["rollback"] = True
        if close:
            limits["close"] = True
    target = store.resolve(ws, ref)
    if gate == "plans":
        if delegate:
            raise UsageError("delegation is given when approving an epic's requirements")
        if despite_open_question and despite_on is not None:
            raise UsageError("pass --despite-open-question or --despite-open-question-on KEY,..., not both")
        return _approve_plans(ws, target, _ALL if despite_open_question else despite_on, only, dry_run, json_out)
    if only is not None or despite_on is not None:
        raise UsageError("--only and --despite-open-question-on go with `orch approve <epic> plans`")
    if epics.is_epic(target.meta or {}):
        # The whole charter is shown before the typed confirmation, and the approval binds the hash of exactly the
        # tickets shown: a child added or changed in between makes it fail, nothing is signed.
        from orch.cli_epic import render_charter
        epic_t = store.load(ws, target.id)[1]
        kids = epics.open_children(ws, epic_t)
        chosen = epics.normalize_delegate(limits)
        content = epics.charter(ws, epic_t, chosen, tickets=kids)["content_hash"]
        t = _human_op(ws, ref, lambda ops, kw: ops.approve(ref, gate, despite_open_question=despite_open_question,
                                                           delegate=limits, **kw),
                      lambda p: "\n".join(render_charter(ws, epic_t, kids, chosen)
                                          + [f"{p.id}: approve the epic as shown ({_short(content)})"]),
                      bound={"expected_hash": content}, dry_run=dry_run, json_out=json_out)
        if dry_run:
            return _dry(ws, t, json_out, f"{t.id}: would approve the epic ({_short(content)}), status then {t.status}")
        from orch.cli_epic import charter_lines
        text = "\n".join([f"{t.id}: {gate} approved (status {t.status})"]
                         + charter_lines(epics.summary(ws, store.load(ws, t.id)[1])))
        return _out(_view(ws, t), json_out, text)
    if delegate:
        raise UsageError("delegation is given when approving an epic")
    from orch.core.gates import GATE_SECTIONS, gate_hash
    if gate not in GATE_SECTIONS:
        raise UsageError("gate must be requirements or plan")
    cur = store.load(ws, target.id)[1]
    gh = gate_hash(cur, gate)  # what is printed below, read once
    t = _human_op(ws, ref, lambda ops, kw: ops.approve(ref, gate, despite_open_question=despite_open_question, **kw),
                  lambda p: "\n".join(_gated_text(cur, gate)
                                      + [f"{p.id}: approve the {gate} exactly as shown ({_short(gh)});"
                                         f" status then {p.status}"]),
                  bound={"expected_hash": gh}, dry_run=dry_run, json_out=json_out)
    if dry_run:
        return _dry(ws, t, json_out, f"{t.id}: would approve the {gate} ({_short(t.meta['gates'][gate]['hash'])}),"
                                     f" status then {t.status}")
    text = f"{t.id}: {gate} approved (status {t.status})"
    parent = epics.parent_epic(ws, t) if gate == "plan" else None
    more = epics.pending_plans(ws, parent) if parent is not None else []
    if more:  # the routine case on an epic: one confirmation for the rest
        text += f"\n{len(more)} more plan(s) in epic {parent.id} wait: orch approve {parent.id} plans"
    _out(_view(ws, t), json_out, text)


def _approve_plans(ws, target, despite: str | None, only: str | None, dry_run: bool, json_out: bool) -> None:
    """`orch approve <epic> plans` (#27): every child's plan waiting for approval (orch.core.epics.pending_plans,
    `--only` narrows it) is printed in full with its key, title and exact plan hash, then a summary, then one typed
    confirmation (the epic's key) approves each of them, bound to its own hash: a plan changed after it was printed
    is skipped and reported. An open-question line is waived per child (`--despite-open-question-on KEY,...`; the
    bare `--despite-open-question` only when exactly one child has one)."""
    from orch.actor import confirm_typed, require_human_terminal
    from orch.core import epics, store
    from orch.core.events import Actor
    from orch.core.gates import gate_hash, human_questions_in
    from orch.core.lifecycle import require_human
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    from orch.textsafe import visible
    if not epics.is_epic(target.meta or {}):
        raise UsageError(f"{target.id} is not an epic: `plans` approves an epic's children",
                         hint=f"orch approve {target.id} plan")
    require_human(Actor("human", "you", "tty"), "approving gates")  # an agent is refused here, dry run or not
    if not dry_run:
        require_human_terminal("human-only action")
    epic_t = store.load(ws, target.id)[1]
    kids = epics.pending_plans(ws, epic_t)  # read once: printed and hashed from the same objects
    wanted = _keys(ws, only) if only is not None else None
    if wanted is not None:
        if not wanted:
            raise UsageError("--only needs at least one key")
        missing = [k for k in wanted if k not in {t.id for t in kids}]
        if missing:
            raise ValidationError(f"no plan waits for approval on {', '.join(missing)} in {epic_t.id}",
                                  hint=f"orch approve {epic_t.id} plans --dry-run lists the plans waiting")
        kids = [t for t in kids if t.id in wanted]
    if not kids:
        raise ValidationError(f"no plan of {epic_t.id}'s children waits for approval",
                              hint="plans are approved while a child is in progress or waiting; a child still open is "
                                   "covered by approving the epic again, which is possible while the epic is in "
                                   f"backlog or open: orch approve {epic_t.id} requirements")
    expected = {t.id: gate_hash(t, "plan") for t in kids}
    asks = {t.id: human_questions_in(t, "plan") for t in kids}
    asks = {k: v for k, v in asks.items() if v}
    if despite == _ALL:
        if len(asks) > 1:
            raise UsageError("more than one plan has a line that reads as an open question for you: "
                             + ", ".join(asks), hint="name the ones you read and mean: --despite-open-question-on "
                                                     + ",".join(asks))
        waived = list(asks)
    else:
        waived = _keys(ws, despite)
        stray = [k for k in waived if k not in expected]
        if stray:
            raise UsageError(f"--despite-open-question-on names {', '.join(stray)}, not in this batch")
    approved, skipped = Ops(ws, Actor("human", "you", "tty"), dry_run=True).approve_plans(
        epic_t.id, expected, despite=waived)
    lines = []
    for t in kids:
        lines += _gated_text(t, "plan") + [f"  plan hash {expected[t.id]}", ""]
    lines += [f"{sid}: skipped ({why})" for sid, why in skipped]
    ok = {t.id for t in approved}
    summary = [f"{'key':<12} {'plan hash':<18} {'open question':<14} title"] + [
        f"{t.id:<12} {_short(expected[t.id]):<18} {('waived' if t.id in waived else 'yes') if t.id in asks else 'no':<14} "
        f"{visible(t.title)}" for t in kids if t.id in ok]
    waivers = [f"despite an open-question line: {k} (line {visible(asks[k][0][:80])})" for k in waived if k in ok]
    if dry_run:
        return _out({"epic": epic_t.id, "dry_run": True, "plans": expected,
                     "would_approve": [t.id for t in approved], "skipped": dict(skipped), "despite": waived},
                    json_out,
                    "\n".join(lines + summary + waivers + [f"dry run: would approve {len(approved)} plan(s) of "
                                                         f"{epic_t.id}: " + ", ".join(t.id for t in approved)
                                                         + "; nothing was written"]))
    if not approved:
        typer.echo("\n".join(lines), err=json_out)
        raise ValidationError(f"none of the plans of {epic_t.id}'s children can be approved now")
    typer.echo("\n".join(lines + summary + waivers
                         + [f"{epic_t.id}: approve the {len(approved)} plan(s) above exactly as shown, each bound to "
                            "its hash"]), err=json_out)
    actor = confirm_typed(epic_t.id)
    approved, skipped = _ops(ws, actor).approve_plans(
        epic_t.id, {t.id: expected[t.id] for t in approved}, despite=waived)
    _out({"epic": epic_t.id, "approved": {t.id: t.meta["gates"]["plan"]["hash"] for t in approved},
          "skipped": dict(skipped)}, json_out,
         "\n".join([f"{t.id}: plan approved ({_short(t.meta['gates']['plan']['hash'])})" for t in approved]
                   + [f"{sid}: skipped, not approved ({why})" for sid, why in skipped]))


@app.command("request-changes")
def request_changes(ref: str, gate: Annotated[str, typer.Argument(help="requirements | plan")],
                     message: MessageOpt = None, dry_run: DryRunOpt = False, json_out: JsonOpt = False) -> None:
    """Send the requirements or plan back with a message. Human only. The gated text is printed first, and the
    request binds exactly that text."""
    from orch.core import store
    from orch.core.gates import GATE_SECTIONS, gate_hash
    ws = _ws()
    if gate not in GATE_SECTIONS:
        raise UsageError("gate must be requirements or plan")
    cur = store.load(ws, store.resolve(ws, ref).id)[1]
    gh = gate_hash(cur, gate)
    t = _human_op(ws, ref, lambda ops, kw: ops.request_changes(ref, gate, message or "", **kw),
                  lambda p: "\n".join(_gated_text(cur, gate, verb="Requesting changes on")
                                      + [f"{p.id}: request changes on the {gate} as shown ({_short(gh)})"]),
                  bound={"expected_hash": gh}, dry_run=dry_run, json_out=json_out)
    if dry_run:
        return _dry(ws, t, json_out, f"{t.id}: would request changes on the {gate}")
    _out(_view(ws, t), json_out, f"{t.id}: requested changes on {gate}")


@app.command()
def verdict(ref: str, result: Annotated[str, typer.Argument(metavar="done|follow-up")],
            message: MessageOpt = None, dry_run: DryRunOpt = False, json_out: JsonOpt = False,
            skip_release: Annotated[str | None, typer.Option(
                "--skip-release", metavar="REASON",
                help="Close an epic whose signed release has not run, without releasing; says why")] = None) -> None:
    """Close a testing ticket or send it back with a note. Human only. For an epic (done only): every open child's
    criteria and evidence are printed first, and the verdict binds exactly what was printed. An epic whose charter
    signs a release that has not run is closed only with --skip-release REASON (recorded as release_skipped)."""
    from orch.core import epics, factory_release, store
    ws = _ws()
    target = store.resolve(ws, ref)
    if epics.is_epic(target.meta or {}) and result == "done":
        from orch.cli_epic import render_verdict
        epic_t = store.load(ws, target.id)[1]
        kids = epics.open_children(ws, epic_t)
        seen = epics.verdict_hash(kids, ws)
        left = factory_release.unreleased(ws, epic_t)
        if left and not (skip_release or "").strip():
            raise UsageError(f"{factory_release.SKIP_TEXT} ({', '.join(left)} not proven yet)",
                             hint=f"to close it without releasing: orch verdict {target.id} done --skip-release "
                                  "REASON")
        note = ([f"{factory_release.SKIP_TEXT}: closing without releasing ({', '.join(left)} not proven): "
                 f"{skip_release}"] if left else [])
        t = _human_op(ws, ref, lambda ops, kw: ops.verdict(ref, result, message, skip_release=skip_release, **kw),
                      lambda p: "\n".join(render_verdict(kids) + note + [f"{p.id}: accept the epic ({_short(seen)})"]),
                      bound={"expected_hash": seen}, dry_run=dry_run, json_out=json_out)
    else:
        # The criteria and evidence the verdict accepts, read before the preview (the preview's status is the new one)
        # and bound by their hash: evidence an agent edits while the human types is refused.
        from orch.cli_epic import render_verdict
        cur = store.load(ws, target.id)[1]
        seen = epics.verdict_hash([cur], ws)
        t = _human_op(ws, ref, lambda ops, kw: ops.verdict(ref, result, message, **kw),
                      lambda p: "\n".join(render_verdict([cur], epic=False)
                                          + [f"{p.id}: verdict {result} ({_short(seen)}); status then {p.status}"]),
                      bound={"expected_hash": seen}, dry_run=dry_run, json_out=json_out, bind_file=True)
    if dry_run:
        what = "close it as done" if result == "done" else "send it back to in-progress"
        return _dry(ws, t, json_out, f"{t.id}: would {what}")
    _out(_view(ws, t), json_out, f"{t.id}: verdict {result} (status {t.status})")


@app.command()
def move(ref: str, status: str, dry_run: DryRunOpt = False, json_out: JsonOpt = False) -> None:
    """Move a ticket; human-only transitions ask for confirmation."""
    from orch.actor import agent_harness, cli_actor, is_interactive
    ws = _ws()
    if agent_harness() or not is_interactive():
        ops = _ops(ws, cli_actor())
        ops.dry_run = dry_run
        t = ops.move(ref, status)
    else:
        holder = {}

        def call(o, kw):
            holder["ops"] = o
            return o.move(ref, status)
        t = _human_op(ws, ref, call, lambda p: f"{p.id}: move to {status}", dry_run=dry_run, json_out=json_out,
                      bind_file=True)
        ops = holder["ops"]
    _warn(ops)
    if dry_run:
        return _dry(ws, t, json_out, f"{t.id}: would move to {t.status}")
    _out({**_view(ws, t), "warnings": ops.warnings} if ops.warnings else _view(ws, t), json_out, f"{t.id}: moved to {t.status}")


# -- ledger ----------------------------------------------------------------------------

@ledger_app.command("adopt")
def ledger_adopt(
    ref: Annotated[Optional[str], typer.Argument(help="A ticket; or --workspace for every ticket.")] = None,
    workspace: Annotated[bool, typer.Option("--workspace", help="Review every ticket in this workspace.")] = False,
    all_: Annotated[bool, typer.Option("--all", help="Show all, then sign all after one typed confirmation.")] = False,
) -> None:
    """Review decisions this machine's ledger does not hold and sign the ones you confirm. Human only.

    Each item shows the full gated text (or the answer or verdict), its id, and what events.jsonl says. These
    decisions were not made through orch on this machine: sign only what you recognise as yours. Type each item's
    id to sign it, Enter to skip; with --all, type `adopt <n>` after the whole list."""
    from orch.actor import require_human_terminal
    from orch.core import ledger, store
    from orch.core.events import Actor
    require_human_terminal("adopting decisions into the ledger")
    if (ref is None) == (not workspace):
        raise UsageError("name one ticket, or pass --workspace")
    ws = _ws()
    if ref is not None:
        tickets = [store.load(ws, store.resolve(ws, ref).id)[1]]
    else:
        tickets = [store.load(ws, e.id)[1] for e in store.scan(ws) if e.meta is not None]
    items = ledger.unsigned_items(ws, tickets)
    if not items:
        typer.echo("nothing to adopt: every decision here is in the ledger on this machine")
        return
    ops = _ops(ws, Actor("human", "you", "tty"))

    from orch.textsafe import decodes_to_hidden, lines, visible

    def show(n, it):
        what = visible(it.get("gate") or it.get("qid") or it["kind"])
        typer.echo(f"\n[{n}/{len(items)}] {visible(it['ticket'])} {visible(it['title'])} · {it['kind']} {what}"
                   f" · id {it['id']}")
        typer.echo("  not made through orch on this machine (no signed ledger entry here)")
        typer.echo(f"  {visible(it['provenance'])}")
        if it.get("text") is None:
            typer.echo("  the text changed since it was approved: it needs a new approval, not adoption")
        else:
            if decodes_to_hidden(it["text"]) or decodes_to_hidden(it["title"]):
                typer.echo("  it holds hidden or control characters (shown escaped): it cannot be adopted; request"
                           " changes instead")
            for line in lines(it["text"]):
                typer.echo(f"  | {line}")

    signed = 0
    if all_:
        for n, it in enumerate(items, 1):
            show(n, it)
        typed = input(f"\nType `adopt {len(items)}` to sign all {len(items)} decisions shown above: ").strip()
        if typed != f"adopt {len(items)}":
            raise UsageError("confirmation did not match; nothing was signed")
        for it in items:
            try:
                ops.ledger_adopt(it, it["id"])
                signed += 1
            except OrchError as e:
                typer.echo(f"skipped {it['ticket']} {it['id']}: {e.message}", err=True)
    else:
        for n, it in enumerate(items, 1):
            show(n, it)
            typed = input(f"  type {it['id']} to sign it, Enter to skip: ").strip()
            if not typed:
                continue
            try:
                ops.ledger_adopt(it, typed)
                signed += 1
            except OrchError as e:
                typer.echo(f"  not signed: {e.message}", err=True)
    typer.echo(f"signed {signed} of {len(items)}")


@ledger_app.command("repair")
def ledger_repair() -> None:
    """Accept the one signed entry a crash left past the ledger's head record. Human only.

    A crash between appending a decision and rewriting the head record cuts the ledger (every chained decision then
    counts as not verified). When the newest line is a validly signed entry numbered right after the head's count,
    this shows it and, after you type its id, rewrites the head to include it. Any other shape is refused."""
    from orch.actor import require_human_terminal
    from orch.core import ledger
    from orch.core.events import Actor
    from orch.textsafe import visible
    require_human_terminal("repairing the ledger")
    tail = ledger.tail_to_repair()
    typer.echo("The ledger ends in one signed entry that its head record does not count yet:")
    for k in ("n", "kind", "workspace", "ticket", "gate", "qid", "verdict", "actor", "via", "at"):
        if tail.get(k) is not None:
            typer.echo(f"  {k}: {visible(str(tail[k]))}")
    typer.echo("Accept it only if you made this decision just before the crash.")
    typed = input(f"  type {tail['mac'][:8]} to accept it, Enter to cancel: ").strip()
    if not typed:
        typer.echo("nothing changed")
        return
    _ops(_ws(), Actor("human", "you", "tty")).ledger_repair(typed)
    typer.echo("repaired: the head record now includes it")


# -- artifacts -------------------------------------------------------------------------

@artifact_app.command("add")
def artifact_add(ref: str,
                 file: Annotated[Optional[Path], typer.Argument(exists=True, dir_okay=False,
                                                                help="The file to add (or pass --url).")] = None,
                 url: Annotated[Optional[str], typer.Option("--url", help="Link a web page instead: CI run, dashboard, PR check, report (http/https only).")] = None,
                 label: Annotated[Optional[str], typer.Option("--label", help="What it shows, in a few words.")] = None,
                 kind: Annotated[Optional[str], typer.Option("--kind", help="screenshot, report, log, link, dataset, build, diagram or other (guessed when left out).")] = None,
                 task: Annotated[Optional[str], typer.Option("--task", help="The task it belongs to, e.g. T3.")] = None,
                 ac: Annotated[Optional[int], typer.Option("--ac", help="The acceptance criterion it proves, e.g. 2.")] = None,
                 inline: Annotated[bool, typer.Option("--inline", help="Also write a Verification line for --ac that shows it.")] = False,
                 name: Annotated[Optional[str], typer.Option("--name")] = None,
                 replace: Annotated[bool, typer.Option("--replace", help="Overwrite a file of the same name.")] = False,
                 context: Annotated[bool, typer.Option("--context", help="Send it along wherever the ticket is synced (for example to the phone).")] = False,
                 json_out: JsonOpt = False) -> None:
    """Link a file or a URL in the ticket: every screenshot, report, log, dashboard or PR check you produce for it.

    A file is copied into artifacts/<ticket>/ (a file already there is linked in place); a URL is linked, never
    fetched. Examples:
      orch artifact add L-0042 /tmp/login.png --ac 2 --inline --label "Login after the fix"
      orch artifact add L-0042 --url https://github.com/acme/app/actions/runs/123 --kind build --label "CI run"
    """
    if (file is None) == (url is None):
        raise typer.BadParameter("pass a file or --url (one of them)")
    ws = _ws()
    ops = _ops(ws)
    if url is not None:
        item = ops.artifact_link(ref, url, label=label, kind=kind, task=task, ac=ac, inline=inline, context=context)
        _out(item, json_out, f"linked {item['kind']} {item['url']}")
        return
    dest = ops.artifact_add(ref, file, name, context=context, kind=kind, label=label, task=task, ac=ac,
                            inline=inline, replace=replace)
    rel = dest.relative_to(ws.artifacts_dir).as_posix()
    _out({"artifact": rel}, json_out, f"added artifacts/{rel}")


@artifact_app.command("list")
def artifact_list(ref: str, json_out: JsonOpt = False) -> None:
    """List what a ticket links (files, URLs, static notes) and the files in its folder it does not link yet."""
    from orch.core import artifacts as art, store
    ws = _ws()
    t = store.load(ws, store.resolve(ws, ref).id)[1]
    rows = [{**e, "kind": art.kind_of(e)} for e in art.entries(t)]
    rows += [{"name": n, "kind": art.guess_kind(n), "unlinked": True} for n in art.unregistered(ws, t)]
    lines = []
    for r in rows:
        where = r.get("name") or r.get("url") or f"static/{r.get('static')}"
        extra = " ".join(x for x in (f"[{r['task']}]" if r.get("task") else "",
                                      f"[AC{r['ac']}]" if r.get("ac") is not None else "",
                                      "(not linked: orch artifact scan)" if r.get("unlinked") else "") if x)
        lines.append(f"{r['kind']:<10} {where}" + (f" — {r['label']}" if r.get("label") else "") + (f" {extra}" if extra else ""))
    _out(rows, json_out, "\n".join(lines) or "no artifacts")


@artifact_app.command("scan")
def artifact_scan(ref: str, json_out: JsonOpt = False) -> None:
    """Link files that were written straight into artifacts/<ticket>/ or static/<ticket>/."""
    found = _ops(_ws()).artifact_scan(ref)
    _out(found, json_out, ("linked " + ", ".join(found)) if found else "nothing to link")


# -- maintenance -----------------------------------------------------------------------

@app.command()
def check(json_out: JsonOpt = False) -> None:
    """Validate config, tickets, gates, human actions and commits. Exit 5 on errors.

    Human approvals, answers and verdicts are checked against orchestrator/.state/events.jsonl and against this
    machine's approval ledger (in the orch config dir). A clone without the same .state, or another machine, reports
    unverified-* and unsigned-decision findings for decisions made elsewhere: review them with `orch ledger adopt`."""
    from orch.core.check import run_checks
    ws = _ws()
    findings = run_checks(ws) + _uncommitted_finding(ws)
    text = "\n".join(f"{f.level:<7} {f.ticket or '-':<8} {f.code:<24} {f.message}" for f in findings) or "all good"
    _out([f.to_dict() for f in findings], json_out, text)
    if any(f.level == "error" for f in findings):
        raise typer.Exit(5)


def _uncommitted_finding(ws) -> list:
    """#36: orch records (tickets, gates, events, synced instructions) git has not committed. Info only: committing
    is the human's step (or a permitted agent's), and only the CLI asks git, never the dashboard's timer."""
    from orch.core.check import Finding
    from orch.core.gitfiles import few, git_view
    view = git_view(ws)
    if view is None or not view.uncommitted:
        return []
    return [Finding("info", "uncommitted-records", None,
                    f"{len(view.uncommitted)} orch record(s) not committed: {few(view.uncommitted)}")]


@app.command()
def migrate(
    apply: Annotated[bool, typer.Option("--apply", help="Write the changes. Without it nothing is written.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Rewrite what older orch versions wrote into the current format. A dry run unless --apply.

    Moves Proposal and Decisions into Context, writes a worked ticket's Plan checklist as tasks, turns
    ../artifacts/<ticket>/<name> links into artifact:<name>, and gives bare-number trackers a prefix. It never
    touches the approval ledger, the event log or an approved text: a rewrite that would change text a human
    decision is bound to is refused for that ticket and listed. Exit 5 when anything was refused, skipped or
    unreadable, also after --apply wrote the rest. Safe to rerun."""
    from orch.config.load import find_home
    from orch.core import migrate as m
    result = m.plan(find_home())
    written = m.apply(result) if apply and result.items else 0
    refused = bool(result.refused or result.unreadable or result.skipped)
    if json_out:
        _out({"applied": bool(apply), "written": written,
              "changes": [{"file": i.rel, "rules": i.rules} for i in result.items],
              "refused": [{"where": w, "rule": r, "why": y} for w, r, y in result.refused],
              "unreadable": [{"file": f, "why": y} for f, y in result.unreadable],
              "skipped": [{"file": f, "why": y} for f, y in result.skipped],
              "notes": [{"file": i.rel, "note": n} for i in result.items for n in i.notes]
              + [{"file": f, "note": n} for f, n in result.notes]}, True, "")
    else:
        lines = [i.diff() if not apply else f"rewrote {i.rel} ({', '.join(i.rules)})" for i in result.items]
        lines += [f"refused  {w}: {r}: {y}" for w, r, y in result.refused]
        lines += [f"skipped  {f}: {y}" for f, y in result.unreadable + result.skipped]
        lines += [f"note     {i.rel}: {n}" for i in result.items for n in i.notes]
        lines += [f"note     {f}: {n}" for f, n in result.notes]
        lines.append("nothing to migrate" if result.empty() else
                     f"{written} file(s) written" if apply else
                     f"{len(result.items)} file(s) would change; run `orch migrate --apply` to write them")
        typer.echo("\n".join(lines))
    if refused:
        raise typer.Exit(5)


@app.command()
def tidy(json_out: JsonOpt = False) -> None:
    """Delete files in temporary/ older than temporary.max_age_days."""
    from orch.core.maintenance import tidy as do_tidy
    ws = _ws()
    removed = [p.relative_to(ws.home).as_posix() for p in do_tidy(ws)]
    _out(removed, json_out, f"removed {len(removed)} file(s)")


@app.command()
def index() -> None:
    """Regenerate tickets/INDEX.md."""
    from orch.core.maintenance import build_index
    ws = _ws()
    typer.echo(f"wrote {build_index(ws).relative_to(ws.home).as_posix()}")


@app.command()
def rules() -> None:
    """Print the active policy for agent context."""
    from orch.core.rules import render_rules
    typer.echo(render_rules(_ws().config))


@app.command()
def init(
    customer: Annotated[str, typer.Option("--customer", help="Customer name for this workspace.")],
    prefix: Annotated[str, typer.Option("--prefix", help="Local ticket ID prefix.")] = "L",
    directory: Annotated[Path, typer.Option("--dir", help="Workspace root.")] = Path("."),
    harness: Annotated[Optional[list[str]], typer.Option("--harness", help="claude, claude-plugin, copilot, codex, … (repeatable).")] = None,
    tracker: Annotated[Optional[list[str]], typer.Option("--tracker", help="External tracker PREFIX=PATTERN=URL with {key}, or {id} for the pattern's (?P<id>…) group (repeatable).")] = None,
    git_type: Annotated[Optional[str], typer.Option("--git-type", help="github, gitlab, gitlab-selfhosted, bitbucket-server, …")] = None,
    git_base_url: Annotated[Optional[str], typer.Option("--git-base-url", help="Base URL of the git host.")] = None,
    review_term: Annotated[Optional[str], typer.Option("--review-term", help="PR or MR.")] = None,
    agent_may: Annotated[Optional[str], typer.Option("--agent-may", help="What agents may do: commit,push,review or none.")] = None,
    repo: Annotated[Optional[list[str]], typer.Option("--repo", help="Repo NAME or NAME=PATH (repeatable).")] = None,
    adopt: Annotated[bool, typer.Option("--adopt", help="Append the orch block to existing AGENTS.md/CLAUDE.md.")] = False,
    no_instructions: Annotated[bool, typer.Option("--no-instructions", help="Skip AGENTS.md, CLAUDE.md, skills and settings.")] = False,
) -> None:
    """Create orchestrator/ with config.json and the folder layout, then write the agent instructions."""
    from orch.config.answers import build_config, parse_agent_may, parse_repo, parse_tracker
    from orch.config.load import load_config
    from orch.core.fsutil import atomic_write_text
    from orch.core.workspace import Workspace
    from orch.errors import ValidationError

    home = directory.resolve() / "orchestrator"
    cfg_path = home / "config.json"
    if cfg_path.exists():
        raise ValidationError(f"{cfg_path} already exists")
    if not customer.strip():
        raise UsageError("--customer must not be blank", hint="the customer name is part of the workspace's id in the "
                         "approval ledger")
    cfg = build_config(customer=customer, prefix=prefix, harnesses=list(harness or ["claude"]),
                       trackers=[parse_tracker(t) for t in tracker or []], git_type=git_type,
                       git_base_url=git_base_url, review_term=review_term,
                       agent_may=parse_agent_may(agent_may) if agent_may is not None else None,
                       repos=[parse_repo(r) for r in repo or []])
    atomic_write_text(cfg_path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    ws = Workspace(home=home, config=load_config(home))
    ws.ensure_layout()
    from orch.core.gitfiles import write_ignore_block
    write_ignore_block(ws)  # caches and locks stay out of git; records stay in (#36)
    typer.echo(f"initialised {home} (prefix {cfg['id']['prefix']}-)")
    if not no_instructions:
        from orch.instructions.sync import sync_instructions
        for r in sync_instructions(ws, adopt=adopt):
            typer.echo(f"{r.action:<11} {r.path.relative_to(ws.root).as_posix()}")
    if cfg["git"]["repos"]:
        typer.echo("next: run `orch hooks install` to add the commit-message check to the listed repos")
    else:
        typer.echo("next: list your repos with `--repo` or under git.repos in orchestrator/config.json, then run `orch hooks install`")


# -- instructions (sub-project 2) ------------------------------------------------------

instructions_app = typer.Typer(no_args_is_help=True, help="Generated agent instructions, skills and settings.")
app.add_typer(instructions_app, name="instructions")


@instructions_app.command("sync")
def instructions_sync(
    adopt: Annotated[bool, typer.Option("--adopt", help="Append the orch block to existing AGENTS.md/CLAUDE.md files that orch does not manage yet.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change; write nothing.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Write orchestrator/AGENTS.orch.md, the AGENTS.md/CLAUDE.md/Copilot blocks, skills and Claude settings."""
    from orch.instructions.sync import sync_instructions
    ws = _ws()
    results = sync_instructions(ws, dry_run=dry_run, adopt=adopt)
    rows = [{"path": r.path.relative_to(ws.root).as_posix(), "action": r.action} for r in results]
    lines = [f"{r['action']:<11} {r['path']}" for r in rows]
    if any(r["action"] == "needs-adopt" for r in rows):
        lines.append("files marked needs-adopt were left untouched; run `orch instructions sync --adopt` "
                     "to append the orch block at their end")
    if dry_run:
        lines.append("(dry run: nothing written)")
    elif not json_out:
        lines += _commit_hint(ws, [r.path for r in results if r.action in ("created", "updated")])
    _out(rows, json_out, "\n".join(lines))


def _commit_hint(ws, written) -> list[str]:
    """orch never commits (#36): name the files it just changed that git has not committed, so the human (or an
    agent the workspace lets commit) commits them on purpose instead of leaving them dirty on every checkout."""
    from orch.core.gitfiles import changed_and_uncommitted
    if not written:
        return []
    dirty = changed_and_uncommitted(ws, written)
    if not dirty:
        return []
    return [f"orch changed shared files; commit these files: {', '.join(dirty)}"]


# -- git hooks (sub-project 2) ---------------------------------------------------------

hook_app = typer.Typer(no_args_is_help=True, help="Entry points called by git and agent harnesses.")
hooks_app = typer.Typer(no_args_is_help=True, help="Install git hooks into the configured repos.")
app.add_typer(hook_app, name="hook")
app.add_typer(hooks_app, name="hooks")


@hook_app.command("commit-msg")
def hook_commit_msg(file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """git commit-msg hook: exit 1 with reasons if the message breaks this workspace's rules."""
    from orch.hooks.commit_msg import check_message
    problems = check_message(_ws(), file.read_text(encoding="utf-8-sig"))
    if problems:
        typer.echo("orch: commit rejected", err=True)
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        raise typer.Exit(1)


@hook_app.command("pre-commit")
def hook_pre_commit(
    list_only: Annotated[bool, typer.Option("--list", help="Print the record paths that would be staged; stage nothing.")] = False,
) -> None:
    """git pre-commit hook: a commit that stages a ticket also stages orch's own record in the state folder (gate
    snapshots, events.jsonl, counter.json). Only `git add`; orch never commits."""
    from orch.core.gitfiles import stage_records
    for p in stage_records(_ws(), Path.cwd(), dry_run=list_only):
        typer.echo(p)


@hooks_app.command("install")
def hooks_install(
    repo: Annotated[Optional[list[Path]], typer.Option("--repo", help="Repo path (default: git.repos from config).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Install even where a commit-msg hook exists in .git/hooks (that hook is kept as commit-msg.pre-orch and runs after the orch check).")] = False,
    stage_records: Annotated[bool, typer.Option("--stage-records", help="Also install a pre-commit hook that stages orch's record in the state folder whenever a commit stages a ticket (skipped where a pre-commit hook exists; under core.hooksPath it is delegated like commit-msg). A commit that names paths may not carry the staged records.")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Install the commit-msg check into each repo's own hooks directory; other hooks keep working.

    A repo with core.hooksPath set to a folder of its own also gets a commit-msg there (or the call added to its
    existing one) that runs the check; that file is part of the repo and has to be committed."""
    from orch.hooks.install import install_hooks
    ws = _ws()
    rows = [{"repo": str(p), "action": a} for p, a in install_hooks(ws, repo or None, force=force, stage_records=stage_records)]
    _out(rows, json_out, "\n".join(f"{r['action']:<10} {r['repo']}" for r in rows))


# -- Claude Code guard (sub-project 2) --------------------------------------------------

def _guard_error(ws) -> None:
    import traceback
    from orch.clock import stamp_s
    path = ws.state_dir / "guard-errors.log"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{stamp_s()}\n{traceback.format_exc()}\n")


@app.command()
def guard(
    hook_json: Annotated[bool, typer.Option("--hook-json", help="Deny with PreToolUse JSON on stdout and exit 0 instead of exit 2 (plugin hook).")] = False,
) -> None:
    """Claude Code PreToolUse hook: exit 2 (or a JSON deny with --hook-json) blocks direct ticket edits and git actions this workspace forbids."""
    import os
    from orch.core.workspace import Workspace
    from orch.hooks import guard as guard_mod
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    start = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    try:
        ws = Workspace.open(start)
    except UsageError:
        return  # not an orch workspace: nothing to guard
    except OrchError as e:
        typer.echo(f"orch guard: workspace config problem, allowing ({e.message})", err=True)
        return
    except Exception as e:
        typer.echo(f"orch guard: internal error, allowing ({type(e).__name__}: {e})", err=True)
        return
    try:
        decision = guard_mod.evaluate(ws, payload)
    except Exception:
        _guard_error(ws)  # fail open, but leave a trace
        return
    if not decision.allow:
        reason = f"orch guard: {decision.reason}"
        if hook_json:
            # Plugin hook: the wrapper never exits 2 (a broken uv would then block every tool call),
            # so the deny travels as JSON on exit 0, which Claude Code reads as permissionDecision.
            typer.echo(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                          "permissionDecision": "deny",
                                                          "permissionDecisionReason": reason}}))
            return
        typer.echo(reason, err=True)
        raise typer.Exit(2)


# -- Claude Code SessionStart (sub-project 2) -------------------------------------------

@hook_app.command("session-start")
def hook_session_start() -> None:
    """Claude Code SessionStart hook: print the active rules, claimed tickets and what waits on the human."""
    from orch.actor import session_id
    from orch.hooks.session_start import session_start_text
    session = session_id()
    if not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except (OSError, ValueError):  # unreadable stdin or not JSON: fall back to the environment
            payload = {}
        if isinstance(payload, dict) and payload.get("session_id"):
            session = str(payload["session_id"])
    try:
        ws = _ws()
    except OrchError:
        from orch.onboarding import outside_workspace_hint
        try:
            hint = outside_workspace_hint(Path.cwd())
        except Exception:
            hint = None  # never break a session start over onboarding
        if hint:
            typer.echo(hint)
        return
    typer.echo(session_start_text(ws, session))


# -- dashboard (sub-project 3) ----------------------------------------------------------

def _lan_ip() -> str:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _run_updates(*, check_only: bool, force: bool) -> None:
    from orch import update
    from orch.cli_addon import review_text

    def confirm(name: str) -> bool:
        try:
            return input(f"Type {name} to trust it, or press Enter to leave it off: ").strip() == name
        except EOFError:
            return False
    update.run(check_only=check_only, ask=input, review_text=review_text, confirm=confirm, out=typer.echo, force=force)


@app.command("update")
def update_cmd(check_only: Annotated[bool, typer.Option("--check", help="Only say what is out of date.")] = False) -> None:
    """Update orch-core and the custom addons: check, ask once, apply. A new addon version that asks for no new
    permissions is trusted again; otherwise you see what changed first."""
    from orch.actor import require_human_terminal
    require_human_terminal("updating orch", hint="run `orch update` in your own terminal")
    _run_updates(check_only=check_only, force=True)


@app.command()
def serve(
    host: Annotated[Optional[str], typer.Option("--host", help="Bind address (default from config).")] = None,
    port: Annotated[Optional[int], typer.Option("--port", help="Port (default from config).")] = None,
    lan: Annotated[bool, typer.Option("--lan", help="Listen on all interfaces, e.g. for your phone.")] = False,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open a browser.")] = False,
    no_update: Annotated[bool, typer.Option("--no-update", help="Do not offer to update orch and its addons.")] = False,
) -> None:
    """Start the local dashboard. Every write goes through the same rules as the CLI."""
    import secrets
    import webbrowser

    from orch.actor import require_human_terminal
    require_human_terminal("starting the dashboard", hint="the dashboard is for the human: run it in your own terminal")
    if not no_update:
        try:
            _run_updates(check_only=False, force=False)
        except OrchError as e:  # an update problem never keeps the dashboard from starting
            typer.echo(f"update skipped: {e.message}")
    try:
        import uvicorn
        from orch.dashboard.app import create_app
    except ImportError as e:
        raise UsageError("the dashboard needs extra packages",
                         hint="uv tool install 'orch-core[dashboard]' (in a checkout: uv sync --extra dashboard)") from e
    ws = _ws()
    cfg = ws.config["dashboard"]
    bind = host or ("0.0.0.0" if lan else cfg["host"])
    bind_port = port or cfg["port"]
    token = secrets.token_urlsafe(24)
    shown = _lan_ip() if bind == "0.0.0.0" else bind
    url = f"http://{shown}:{bind_port}/?token={token}"
    typer.echo(f"orch dashboard: {url}")
    if lan:
        typer.echo("reachable from your network: anyone with this link can act as you")
    if not no_open and not lan:
        try:
            webbrowser.open(url)
        except Exception:
            pass  # headless machine: the printed link is enough
    from orch.dashboard.factory_runner import configure_logging
    configure_logging()  # the AI Factory runner's lines reach this terminal
    uvicorn.run(create_app(ws, token, port=bind_port), host=bind, port=bind_port, log_level="warning")


# -- ticket schema (tools such as phone apps) --------------------------------------------

@schema_app.command("ticket")
def schema_ticket(json_out: JsonOpt = True) -> None:
    """Print the JSON Schema of the ticket document (always JSON)."""
    from orch.core.schema import ticket_schema
    _out(ticket_schema(), True, "")


@schema_app.command("example")
def schema_example(json_out: JsonOpt = True) -> None:
    """Print an example ticket document in the current schema version (always JSON)."""
    from orch.core.schema import example_document
    _out(example_document(), True, "")


# -- feedback about orch itself (#37) -----------------------------------------------------

feedback_app = typer.Typer(no_args_is_help=True, help="Report orch friction to a local queue; the human reviews and files it.")
app.add_typer(feedback_app, name="feedback")


@feedback_app.command("add")
def feedback_add(
    command: Annotated[str, typer.Option("--command", help="The orch command that was confusing or broken.")] = "",
    message: MessageOpt = None,
    file: FileOpt = None,
    error: Annotated[str, typer.Option("--error", help="The error or output orch printed.")] = "",
) -> None:
    """Agents: report an orch command, guard decision or skill step that is confusing or broken, once, then carry on.

    The report is redacted (no paths, customer, repo or tracker names, ticket ids or text) and saved in the orch
    config dir on this machine, never in the repository; nothing is sent anywhere. The human reviews it."""
    from orch import feedback
    from orch.actor import agent_harness
    try:
        ws = _ws()
    except OrchError:
        ws = None  # orch may be broken exactly here: report without a workspace
    outcome, report = feedback.add(ws, command=command, problem=_text(message, file), error=error,
                                   harness=agent_harness() or "terminal")
    if outcome == "off":
        typer.echo("orch feedback is turned off in this workspace; nothing saved. Carry on with your work.")
    elif outcome == "limit":
        typer.echo(f"orch feedback: the daily limit ({feedback.DAILY_LIMIT} reports) is reached; nothing saved. "
                   "Carry on with your work.")
    else:
        word = "already reported; counted again" if outcome == "counted" else "saved"
        typer.echo(f"orch feedback {report['id']} {word} (on this machine only; the user reviews it with "
                   "`orch feedback list`). Carry on with your work.")


def _feedback_human(what: str) -> None:
    from orch.actor import require_human_terminal
    require_human_terminal(what, hint="feedback reports are for the human: run this in your own terminal")


@feedback_app.command("list")
def feedback_list(json_out: JsonOpt = False) -> None:
    """Human only: the feedback reports agents saved on this machine."""
    from orch import feedback
    _feedback_human("reading feedback reports")
    rows = feedback.reports()
    lines = [f"{r['id']}  {r.get('status', ''):<9} x{r.get('count', 1):<3} {r.get('created', '')}  "
             f"{feedback.title(r)}" for r in rows]
    _out(rows, json_out, "\n".join(lines) or "no feedback reports")


@feedback_app.command("show")
def feedback_show(report_id: Annotated[str, typer.Argument(help="fb-… id from `orch feedback list`.")]) -> None:
    """Human only: one report, as it would be filed."""
    from orch import feedback
    _feedback_human("reading feedback reports")
    r = feedback.get(report_id)
    typer.echo(f"{feedback.title(r)}\n\n{feedback.body(r)}")


@feedback_app.command("file")
def feedback_file(
    report_id: Annotated[str, typer.Argument(help="fb-… id from `orch feedback list`.")],
    note: Annotated[str, typer.Option("--note", help="Your own words to add (not redacted: you choose what to share).")] = "",
    repo: Annotated[str, typer.Option("--repo", help="owner/name of the orch-core repository.")] = "",
) -> None:
    """Human only: show the exact issue text, then create it on the orch-core repository with gh after you type FILE."""
    from orch import feedback
    _feedback_human("filing feedback")
    r = feedback.get(report_id)
    target = repo or feedback.REPO
    typer.echo(f"Repository: {target}\nTitle: {feedback.title(r)}\n\n{feedback.body(r, note)}")
    if input(f"Type FILE to create this issue on {target} with gh: ").strip() != "FILE":
        raise UsageError("not filed; nothing was sent")
    url = feedback.file_issue(r, note, target)
    typer.echo(f"filed: {url}" if url else "filed")


@feedback_app.command("dismiss")
def feedback_dismiss(report_id: Annotated[str, typer.Argument(help="fb-… id from `orch feedback list`.")]) -> None:
    """Human only: keep a report but mark it dismissed (repeats then only raise its count)."""
    from orch import feedback
    _feedback_human("dismissing feedback")
    feedback.set_status(feedback.get(report_id), "dismissed")
    typer.echo(f"dismissed {report_id}")


# -- onboarding (store plugin) -----------------------------------------------------------

@app.command()
def doctor(
    fix: Annotated[bool, typer.Option("--fix", help="Write the orch block of orchestrator/.gitignore (nothing else).")] = False,
    json_out: JsonOpt = False,
) -> None:
    """Check this repository's orch setup and say how to fix what is missing. Changes no files or settings (except
    the orch block of orchestrator/.gitignore with --fix); may recreate missing orchestrator folders."""
    from orch.onboarding import doctor as run_doctor
    if fix:
        from orch.core.gitfiles import write_ignore_block
        ws = _ws()
        action = write_ignore_block(ws)
        if not json_out:
            typer.echo(f"{action:<11} {(ws.home / '.gitignore').relative_to(ws.root).as_posix()}")
    checks = run_doctor() + [_actor_check()] + _customer_check()
    lines = [f"{'✓' if c.ok else '✗'} {c.code}: {c.message}" + (f"\n    → {c.fix}" if c.fix and not c.ok else "")
             for c in checks]
    _out([c.to_dict() for c in checks], json_out, "\n".join(lines))


def _customer_check():
    """A blank customer still works (the ledger id is then "|<prefix>"), but two such workspaces with one prefix
    share a ledger id; say how to name it."""
    from orch.onboarding import Check
    try:
        ws = _ws()
    except OrchError:
        return []
    if str(ws.config.get("customer") or "").strip():
        return []
    return [Check("customer", False, "the workspace has no customer name",
                  'set "customer": "<name>" in orchestrator/config.json; decisions signed before the change then '
                  "need orch ledger adopt --workspace")]


def _actor_check():
    """Informational: who orch thinks runs this command, and why (so a human refused as an agent sees the reason)."""
    from orch.actor import agent_harness, process_evidence
    from orch.onboarding import Check
    ev = process_evidence()
    chain = " ← ".join(ev["chain"]) if ev["chain"] is not None else "unreadable"
    harness = agent_harness()
    if harness:
        msg = (f"an agent harness is detected ({harness}): human-only actions are refused here; "
               f"process chain: {chain}")
    else:
        msg = f"no agent harness detected: human-only actions are allowed from an interactive terminal; process chain: {chain}"
    return Check("actor", True, msg)


@app.command()
def setup(
    item: Annotated[Optional[str], typer.Argument(help="Doctor code to hide, e.g. repos or hooks.")] = None,
    dismiss_: Annotated[bool, typer.Option("--dismiss", help="Stop the setup hint for this repo, or hide one open item.")] = False,
) -> None:
    """Show what is left to set up, or dismiss the setup hint (the orch-setup skill does the guided setup)."""
    from orch.onboarding import dismiss, doctor as run_doctor, git_root, validate_item_code
    if item is not None and not dismiss_:
        raise UsageError("ITEM only makes sense together with --dismiss", hint="orch setup --dismiss ITEM")
    if dismiss_:
        if item is None:
            root = git_root(Path.cwd()) or Path.cwd()
            dismiss(root)
            typer.echo(f"no more setup hints for {root}")
            return
        validate_item_code(item)
        root = _ws().root
        dismiss(root, item)
        typer.echo(f"hidden: {item}")
        return
    checks = run_doctor()
    for c in checks:
        typer.echo(f"{'✓' if c.ok else '✗'} {c.code}: {c.message}")
    typer.echo("For a guided setup ask your agent to use the orch-setup skill, "
               "or run /orch-core:setup in Claude Code.")
