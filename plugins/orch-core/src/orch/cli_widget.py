"""`orch widget …`: ticket widgets, format orch.widgets.v1 (docs/widgets.md)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from orch.errors import UsageError

widget_app = typer.Typer(no_args_is_help=True,
                         help="Ticket widgets: small visual blocks (```orch) in Context, Current state, Verification, "
                              "Findings. Cite where numbers came from; never invent them.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
# Sections `orch widget add` writes: where widgets may stand and `orch section set` can write.
ADD_SECTIONS = ("Context", "Current state", "Verification", "Findings")


def _cli():
    from orch import cli
    return cli, cli._ws()


def _templates(home, moment: str | None = None) -> list[dict]:
    from orch.widgets import registry
    rows = [{"name": n, "layer": "widget", "origin": t["origin"], "title": t.get("title", ""),
             "moment": t.get("moment"), "versions": sorted(t["versions"], key=lambda v: int(v) if v.isdigit() else 0)}
            for n, t in registry.templates(home).items()]
    return [r for r in rows if moment is None or r["moment"] == moment]


@widget_app.command("types")
def types_(json_out: JsonOpt = False) -> None:
    """Core types and templates, with the moment each serves and an example."""
    from orch.widgets import registry
    cli, ws = _cli()
    rows = registry.describe() + _templates(ws.home)
    cli._out(rows, json_out, "\n".join(f"{r['name']:<10} {r['layer']:<7} {r.get('moment') or '-':<10} "
                                       + json.dumps(r.get("example"), ensure_ascii=False) if r["layer"] == "core"
                                       else f"{r['name']:<10} widget  {r.get('moment') or '-':<10} v{','.join(r['versions'])}"
                                       for r in rows))


@widget_app.command("show")
def show(name: Annotated[str, typer.Argument(help="A core type, or a template as name or name@version.")],
         json_out: JsonOpt = False) -> None:
    """Schema and example of a core type; versions, schemas and examples of a template."""
    from orch.widgets import registry
    cli, ws = _cli()
    if name in registry.core_types():
        mod = registry.core_types()[name]
        data = {"name": name, "layer": "core", "moment": mod.MOMENT, "schema": registry.block_schema(name),
                "example": mod.EXAMPLE}
    else:
        spec = registry.templates(ws.home).get(name.partition("@")[0])
        if spec is None:
            raise UsageError(f"no core type or template {name!r}", hint="see `orch widget types`")
        version = name.partition("@")[2]
        data = spec
        if version:
            if version not in spec["versions"]:
                raise UsageError(f"template {spec['name']} has no version {version}",
                                 hint="versions: " + ", ".join(spec["versions"]))
            folder = Path(spec["folder"])
            try:
                example = json.loads((folder / "example.json").read_text(encoding="utf-8")).get(version)
            except (OSError, ValueError):
                example = None
            data = {**spec, "versions": {version: spec["versions"][version]}, "example": example,
                    "body": str(folder / f"v{version}.html")}
    cli._out(data, json_out, json.dumps(data, ensure_ascii=False, indent=2))


@widget_app.command("list")
def list_(moment: Annotated[Optional[str], typer.Option("--for", help="Only templates for this moment.")] = None,
          json_out: JsonOpt = False) -> None:
    """Templates (built-in and orchestrator/widgets/): versions, how many blocks in this workspace use each (per
    version) and whether none was touched in the last 30 days."""
    from datetime import timedelta

    from orch.clock import now, parse_stamp
    from orch.widgets.validate import usage
    cli, ws = _cli()
    used = usage(ws)["widget"]
    cutoff = now() - timedelta(days=30)
    rows = []
    for r in _templates(ws.home, moment):
        u = used.get(r["name"], {})
        try:
            recent = bool(u.get("last")) and parse_stamp(u["last"]) >= cutoff
        except ValueError:
            recent = False
        rows.append({**r, "uses": u.get("uses", 0), "uses_by_version": u.get("versions", {}),
                     "tickets": u.get("tickets", []), "unused_30_days": not recent})
    from orch.widgets import registry
    bad = [{**b, "layer": "widget", "versions": []} for b in registry.template_problems(ws.home)]
    cli._out(rows + bad, json_out, "\n".join(
        [f"{r['name']:<20} v{','.join(r['versions']):<6} {r['uses']:>3} uses  "
         f"{'unused 30 days  ' if r['unused_30_days'] else ''}{r['title']}" for r in rows]
        + [f"{b['name']:<20} skipped: {b['problem']} ({b['path']})" for b in bad]) or "no templates")


def _data(data: str | None, file: Path | None) -> dict:
    if data is not None and file is not None:
        raise UsageError("pass --data or --file, not both")
    raw = file.read_text(encoding="utf-8") if file is not None else (data or "{}")
    from orch.widgets.blocks import load_strict
    obj, error = load_strict(raw)
    if error:
        raise UsageError(f"the data is {error}", hint="one strict JSON object")
    return obj


@widget_app.command("add")
def add(ref: str,
        section: Annotated[str, typer.Option("--section", help="Context, Current state, Verification or Findings.")],
        type_: Annotated[Optional[str], typer.Option("--type", help="A core type (orch widget types).")] = None,
        widget: Annotated[Optional[str], typer.Option("--widget", help="A template, name@version.")] = None,
        html: Annotated[Optional[str], typer.Option("--html", help="A one-off page: an artifact of this ticket.")] = None,
        data: Annotated[Optional[str], typer.Option("--data", help="The data as one JSON object.")] = None,
        file: Annotated[Optional[Path], typer.Option("--file", exists=True, dir_okay=False,
                                                     help="Read the data from a JSON file.")] = None,
        title: Annotated[Optional[str], typer.Option("--title")] = None,
        source: Annotated[Optional[str], typer.Option("--source", help="Where the numbers came from.")] = None,
        caption: Annotated[Optional[str], typer.Option("--caption")] = None,
        wid: Annotated[Optional[str], typer.Option("--id", help="Anchor id [a-z0-9-], unique in the ticket.")] = None,
        json_out: JsonOpt = False) -> None:
    """Validate a block, fill in file digests and append it to a section (through `orch section set`'s path)."""
    from orch.core import store
    from orch.widgets import Ctx, render_text
    from orch.widgets.artifacts import fill_digests
    from orch.widgets.blocks import MAX_BLOCKS, make_block, ticket_blocks
    from orch.widgets.validate import validate
    cli, ws = _cli()
    canonical = {s.lower(): s for s in ADD_SECTIONS}.get(section.strip().lower())
    if canonical is None:
        raise UsageError(f"widgets are not added to {section!r}", hint="one of: " + ", ".join(ADD_SECTIONS)
                         + " (gated sections, Tasks and the Log never hold widgets)")
    if sum(x is not None for x in (type_, widget, html)) != 1:
        raise UsageError("pass exactly one of --type, --widget or --html")
    path, ticket = store.load(ws, ref)
    payload = _data(data, file)
    if type_ is not None:
        block = {"type": type_, **payload}
    elif widget is not None:
        from orch.widgets import registry
        _, current = registry.template_state(ws.home, widget, None)  # the pin: the version as it is now
        block = {"widget": widget, **({"sha256": current} if current else {}), **({"data": payload} if payload else {})}
    else:
        name = html if html.startswith("artifacts/") else f"artifacts/{ticket.id}/{html}"
        block = {"html": name, "sha256": "", **({"data": payload} if payload else {})}
    block.update({k: v for k, v in (("title", title), ("source", source), ("caption", caption), ("id", wid)) if v})
    missing = fill_digests(ws, ticket.id, block)
    text = json.dumps(block, ensure_ascii=False)  # one line: a fence line can never appear inside the JSON
    candidate = make_block(canonical, 0, text)
    errors = ([_problem(f"{m} is not a file of {ticket.id} (add it with `orch artifact add` first)") for m in missing]
              or [p for p in validate(candidate, ticket, ws=ws) if p.level == "error"])
    existing = ticket_blocks(ticket, path.read_text(encoding="utf-8"))
    if wid and any((b.data or {}).get("id") == wid for b in existing):
        errors.append(_problem(f"id {wid!r} is already used in {ticket.id}"))
    if len(existing) >= MAX_BLOCKS:
        errors.append(_problem(f"{ticket.id} already has {MAX_BLOCKS} widgets"))
    if errors:
        raise UsageError("widget refused: " + "; ".join(p.message for p in errors),
                         hint=f"see `orch widget show {type_ or (widget or '').partition('@')[0] or 'types'}`")
    t = cli._ops(ws).append_section(ticket.id, canonical, f"```orch\n{text}\n```")
    candidate.index = len(existing)
    out = {"ticket": t.id, "section": canonical, "index": candidate.index, "block": block,
           "text": render_text(candidate, Ctx.of(ws, t))}
    cli._out(out, json_out, f"{t.id}: widget added to {canonical} (#{candidate.index})")


def _problem(message: str):
    from orch.widgets.blocks import Problem
    return Problem("widget-schema", message)


@widget_app.command("check")
def check(ref: Annotated[Optional[str], typer.Argument(help="One ticket; default every ticket.")] = None,
          json_out: JsonOpt = False) -> None:
    """widget-parse, widget-schema, widget-place and widget-digest findings, in tickets and (for the whole workspace)
    in the local wiki pages, named `<page path>:<line>`. Exit 5 on errors."""
    from orch.widgets import pages
    from orch.widgets.validate import findings
    cli, ws = _cli()
    rows = findings(ws, ref)
    if ref is None:
        rows += [{**r, "page": True} for r in pages.findings(ws)]
    if ref is None:  # a malformed template is left out everywhere: say which and why
        from orch.widgets import registry
        rows += [{"ticket": "-", "index": None, "section": b["path"], "line": 0, "code": "widget-template",
                  "level": "warning", "message": f"template {b['name']} skipped: {b['problem']}"}
                 for b in registry.template_problems(ws.home)]
    cli._out(rows, json_out, "\n".join(
        f"{r['level']:<7} {'page':<8} {r['code']:<14} {r['section']}:{r['line']}: {r['message']}" if r.get("page") else
        f"{r['level']:<7} {r['ticket']:<8} {r['code']:<14} {r['section']}, line {r['line']}: {r['message']}"
        for r in rows) or "all good")
    if any(r["level"] == "error" for r in rows):
        raise typer.Exit(5)


@widget_app.command("render")
def render(ref: str,
           text: Annotated[bool, typer.Option("--text", help="Text alternatives (the default).")] = False,
           html: Annotated[bool, typer.Option("--html", help="Standalone HTML documents.")] = False,
           wid: Annotated[Optional[str], typer.Option("--id", help="Only this block (its id or index).")] = None,
           out: Annotated[Optional[Path], typer.Option("--out", file_okay=False,
                                                       help="Write one <ID>-<key>.html per block into this folder.")] = None,
           json_out: JsonOpt = False) -> None:
    """The text alternative or the standalone document (CSP, theme, CSS inline) of a ticket's widgets."""
    from orch.widgets import Ctx, render_document, render_text, ticket_blocks
    from orch.widgets.validate import tickets
    if text and html:
        raise UsageError("pass --text or --html, not both")
    cli, ws = _cli()
    path, ticket, raw = next(tickets(ws, ref))
    blocks = [b for b in ticket_blocks(ticket, raw) if wid is None or wid in (b.key, str(b.index))]
    if wid is not None and not blocks:
        raise UsageError(f"{ticket.id} has no widget {wid!r}")
    ctx = Ctx.of(ws, ticket)
    if not html:
        rows = [{"index": b.index, "id": (b.data or {}).get("id"), "section": b.section, "text": render_text(b, ctx)}
                for b in blocks]
        cli._out(rows, json_out, "\n\n".join(f"#{r['index']} {r['section']}\n{r['text']}" for r in rows)
                 or f"{ticket.id} has no widgets")
        return
    docs = [(b, render_document(b, ctx)) for b in blocks]
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        files = []
        for b, doc in docs:
            target = out / f"{ticket.id}-{b.key}.html"
            target.write_text(doc, encoding="utf-8")
            files.append(str(target))
        cli._out(files, json_out, "\n".join(files) or f"{ticket.id} has no widgets")
    elif len(docs) == 1 and not json_out:
        typer.echo(docs[0][1], nl=False)
    else:
        if not json_out:
            raise UsageError(f"{ticket.id} has {len(docs)} widgets", hint="pick one with --id, or pass --out DIR or --json")
        cli._out([{"index": b.index, "id": (b.data or {}).get("id"), "document": d} for b, d in docs], True, "")


def infer_schema(value) -> dict:
    """A strict JSON Schema that the example `value` passes: every key required, no others allowed."""
    if isinstance(value, dict):
        return {"type": "object", "additionalProperties": False, "required": list(value),
                "properties": {k: infer_schema(v) for k, v in value.items()}}
    if isinstance(value, list):
        return {"type": "array", **({"items": infer_schema(value[0])} if value else {})}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    return {"type": "string"} if isinstance(value, str) else {"type": "null"}


@widget_app.command("promote")
def promote(ref: str,
            artifact: Annotated[str, typer.Argument(help="The one-off page: an artifact of this ticket.")],
            name: Annotated[str, typer.Option("--name", help="The template's name [a-z0-9-].")],
            version_bump: Annotated[bool, typer.Option("--version-bump", help="Add the next version to an existing "
                                                       "workspace template of that name.")] = False,
            moment: Annotated[str, typer.Option("--moment", help="The moment it serves.")] = "report",
            json_out: JsonOpt = False) -> None:
    """Turn a one-off page into a workspace template (orchestrator/widgets/<name>/): v1.html, widget.json with a
    schema inferred from the block's data, example.json."""
    import re
    import shutil

    from orch.core import store
    from orch.widgets import registry
    from orch.widgets.artifacts import resolve
    from orch.widgets.blocks import ticket_blocks
    cli, ws = _cli()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
        raise UsageError(f"{name!r} is not a template name", hint="1-40 of a-z, 0-9 and -, starting with a letter or digit")
    if moment not in registry.MOMENTS:
        raise UsageError(f"unknown moment {moment!r}", hint="one of: " + ", ".join(registry.MOMENTS))
    path, ticket = store.load(ws, ref)
    art = artifact if artifact.startswith("artifacts/") else f"artifacts/{ticket.id}/{artifact}"
    page = resolve(ws, ticket.id, art)
    if page is None or page.suffix.lower() not in (".html", ".htm"):
        raise UsageError(f"{art} is not an HTML file of {ticket.id}", hint="add it with `orch artifact add` first")
    block = next((b.data for b in ticket_blocks(ticket, path.read_text(encoding="utf-8"))
                  if isinstance(b.data, dict) and b.data.get("html") == art), {})
    data = block.get("data")
    existing = registry.templates(ws.home).get(name)
    folder = ws.home / "widgets" / name
    if existing and not version_bump:
        raise UsageError(f"a template {name!r} already exists ({existing['origin']})",
                         hint="pick another --name, or pass --version-bump to add its next version")
    if existing and existing["origin"] != "workspace":
        raise UsageError(f"{name!r} is a built-in template", hint="pick another --name")
    if existing:
        spec = json.loads((folder / "widget.json").read_text(encoding="utf-8"))
        examples = json.loads((folder / "example.json").read_text(encoding="utf-8")) \
            if (folder / "example.json").is_file() else {}
        version = str(max((int(v) for v in spec.get("versions", {}) if v.isdigit()), default=0) + 1)
    else:
        spec = {"name": name, "title": block.get("title") or name, "description": block.get("caption") or "",
                "moment": moment, "libs": block.get("libs", []), "min_height": int(block.get("height") or 160),
                "versions": {}}
        examples, version = {}, "1"
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              **(infer_schema(data) if isinstance(data, dict) else {"type": "object"})}
    spec["versions"][version] = {"schema": schema, "notes": f"promoted from {ticket.id} {art}"}
    examples[version] = data if isinstance(data, dict) else {}
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(page, folder / f"v{version}.html")
    (folder / "widget.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (folder / "example.json").write_text(json.dumps(examples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out = {"name": name, "version": version, "widget": f"{name}@{version}", "folder": str(folder)}
    cli._out(out, json_out, f"template {name}@{version} written to {folder}\n"
                            f'use it as {{"widget": "{name}@{version}", "data": {{…}}}}')


@widget_app.command("html")
def html_(state: Annotated[str, typer.Argument(help="on | off | status")] = "status", json_out: JsonOpt = False) -> None:
    """Whether agent-written HTML runs in ticket widgets. `on` is the human's decision, signed into the approval
    ledger (run it in your own terminal); `off` anyone may run; a config edit alone never turns it on."""
    from orch.core import ledger
    cli, ws = _cli()
    if state not in ("on", "off", "status"):
        raise UsageError("expected on, off or status")
    if state == "on":
        from orch.actor import confirm_typed, require_human_terminal
        require_human_terminal("turning on agent HTML in widgets")
        typer.echo("Agent-written HTML and scripts will run in sandboxed frames on ticket pages of this workspace.",
                   err=json_out)
        cli._ops(ws, confirm_typed("HTML")).set_widgets_html(True)
    elif state == "off":
        cli._ops(ws).set_widgets_html(False)
    now = ledger.widgets_html_state(ws)
    cli._out({"widgets.html": now}, json_out, {
        "on": "agent HTML is on (signed)", "off": "agent HTML is off",
        "stale": "agent HTML is off in config.json, but the signed on is still in force; run `orch widget html off`",
        "unsigned": "agent HTML is off: config.json asks for it, but no current signed decision for this checkout "
                    "on this machine backs it; "
                    "turn it on with `orch widget html on` in your own terminal"}[now])
