"""`orch epic …` and `orch sprint …` (E2): epics with a charter approval and delegation, sprints as planning
metadata. Creating and linking are options of `orch new` and `orch link`; approving is `orch approve`."""
from __future__ import annotations

from typing import Annotated

import typer

epic_app = typer.Typer(no_args_is_help=True, help="Epics: children, the charter approval, delegation.")
sprint_app = typer.Typer(no_args_is_help=True, help="Sprints defined in the workspace config (planning only).")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _ctx():
    from orch import cli
    return cli, cli._ws()


def _epic(ws, ref: str):
    from orch.core import epics, store
    from orch.errors import UsageError
    t = store.load(ws, ref)[1]
    if not epics.is_epic(t):
        raise UsageError(f"{t.id} is not an epic")
    return t


def release_text(target, long: bool = False) -> str:
    """What a Dark charter signs about releasing: nothing, or up to merge or dev with the recipe on this machine.
    Never production, never closing: the verdict stays the human's."""
    if target in ("merge", "dev"):
        stages = "merge" if target == "merge" else "merge and dev"
        text = (f"releases up to {target} by itself using the recipe on this machine ({stages}, once every child is "
                "Ready; nothing releases to production) and closes nothing: the verdict is yours")
        return ("Release: " + text + ".") if long else text
    if long:
        return "Release: none. Dark releases and closes nothing by itself: the verdict stays yours."
    return "releases and closes nothing (the charter signs no release: the verdict is yours)"


def charter_lines(s: dict) -> list[str]:
    from orch.textsafe import visible
    # the content hash: what the approve prompt and the dashboard show (the epic and its children); the hash that
    # also binds a delegation choice is only in --json ("hash")
    lines = [f"{s['epic']} {visible(s['title'])} · {s['status']} · charter sha256 {s['charter']['content_hash'][7:15]}…"]
    if s["diff"]["epic_changed"]:
        lines.append("  the epic's requirements changed since its approval")
    for c in s["children"]:
        lines.append(f"  {c['id']:<8} {c['status']:<12} {c['state_label']:<34} {visible(c['title'])}")
    for rid in s["diff"]["removed"]:
        lines.append(f"  {rid:<8} left the epic since its approval")
    added = [c["id"] for c in s["children"] if c["state"] == "new" and c["status"] == "backlog"]
    if s["approved"] and added:  # a child added later is approved on its own: the epic and its other children stay as they are
        lines.append("  approve just the new " + ("child" if len(added) == 1 else "children") + " without the epic: "
                     + ", ".join(f"orch approve {i}" for i in added))
    d = s["delegation"]
    if d:
        state = ("paused" if d["paused"] else "suspended (epic changed)" if d["epic_changed"]
                 else "stopped (budget used up)" if d.get("expired") else "active")
        name = "Dark AI Factory" if d.get("dark") else "AI Factory" if d.get("factory") else "delegation"
        lines.append(f"  {name} {state}: up to {d['max_children']} "
                     f"children of size ≤ {d['max_size']}"
                     + (f" or {d['max_hours']} hours from {d['at']}" if d.get("max_hours") else ""))
        if d.get("dark"):
            lines.append("  runs without asking you: only shell commands the Dark profile lists; "
                         + release_text(d.get("release")))
    for a in s["auto_approvals"]:
        lines.append(f"  auto-approved {a['ticket']} {a['gate']} by {a['actor']} at {a['at']}"
                     + ("" if a["valid"] else " (no longer valid)"))
    r = s["rollup"]
    lines.append(f"  children done {r['done']}/{r['total']} · criteria proven {r['ac_proven']}/{r['ac_total']}")
    return lines


@epic_app.command("show")
def show(ref: str, json_out: JsonOpt = False) -> None:
    """The epic's children and their state against its approval, the delegation and its audit."""
    from orch.core import epics
    cli, ws = _ctx()
    s = epics.summary(ws, _epic(ws, ref))
    cli._out(s, json_out, "\n".join(charter_lines(s)))


@epic_app.command("auto-approve")
def auto_approve(ref: str, json_out: JsonOpt = False) -> None:
    """Agents: approve a child you wrote under the epic's delegation (requirements and plan, once, within limits)."""
    cli, ws = _ctx()
    t = cli._ops(ws).epic_auto_approve(ref)
    cli._out(cli._view(ws, t), json_out, f"{t.id}: auto-approved under the epic's delegation (status {t.status})")


@epic_app.command("pause")
def pause(ref: str, dry_run: Annotated[bool, typer.Option(
              "--dry-run", help="Check everything and print what would happen; asks nothing and writes nothing.")] = False,
          json_out: JsonOpt = False) -> None:
    """Stop further auto-approvals under the epic's delegation (those made so far stay). Human only."""
    cli, ws = _ctx()
    t = cli._human_op(ws, ref, lambda ops, kw: ops.epic_pause(ref),
                      lambda p: f"{p.id}: pause the delegation (auto-approvals made so far stay)",
                      dry_run=dry_run, json_out=json_out, bind_file=True)
    if dry_run:
        return cli._dry(ws, t, json_out, f"{t.id}: would pause the delegation")
    cli._out(cli._view(ws, t), json_out, f"{t.id}: delegation paused")


@sprint_app.command("list")
def sprint_list(json_out: JsonOpt = False) -> None:
    """The sprints defined in orchestrator/config.json."""
    from orch.core import sprints
    cli, ws = _ctx()
    rows = sprints.all_sprints(ws)
    cli._out(rows, json_out, "\n".join(f"{s['id']:<10} {s['start']} … {s['end']}  {s['name']}" for s in rows)
             or "no sprints defined (add `sprints` to orchestrator/config.json)")


@sprint_app.command("current")
def sprint_current(json_out: JsonOpt = False) -> None:
    """The sprint that contains today."""
    from orch.core import sprints
    cli, ws = _ctx()
    s = sprints.current(ws)
    cli._out(s, json_out, f"{s['id']} {s['name']} ({s['start']} … {s['end']})" if s else "no current sprint")


def _block(out: list[str], title: str, text: str, indent: str) -> None:
    from orch.textsafe import lines
    out.append(f"{indent}{title}:")
    body = lines(text) if text.strip() else ["(empty)"]
    out.extend(f"{indent}  | {line}" for line in body)


def render_charter(ws, epic, kids, delegate) -> list[str]:
    """Everything an epic approval binds, for the terminal, before the typed confirmation: the epic's gated text
    and size/type, each open child's title, requirements sections, size/type and plan, the children that left,
    and the delegation choice. Ticket text is shown with hidden characters escaped (orch.textsafe)."""
    from orch.core import epics
    from orch.core.gates import gate_meta, gate_parts
    from orch.textsafe import visible
    diff = epics.charter_diff(ws, epic, tickets=kids)
    out = [f"Approving epic {epic.id}: {visible(epic.title)}"]
    if diff["epic_changed"]:
        out.append("  the epic's own text changed since your last approval")
    for name, _ in gate_parts(epic, "requirements"):
        _block(out, name, epic.section(name), "  ")
    out.append("  " + " · ".join(f"{k}: {visible(v)}" for k, v in gate_meta(epic, "requirements")))
    out.append(f"Children covered ({len(kids)}):" if kids else "No open children: the approval covers the epic only.")
    for t in kids:
        change = diff["children"].get(t.id, "new")
        out.append(f"- {t.id} {visible(t.title)} · {t.status}" + (f" · {change}" if diff["previous"] else ""))
        for name, _ in gate_parts(t, "requirements"):
            _block(out, name, t.section(name), "    ")
        out.append("    " + " · ".join(f"{k}: {visible(v)}" for k, v in gate_meta(t, "requirements")))
        _block(out, "Plan", t.section("Plan"), "    ")
    for rid in diff["removed"]:
        out.append(f"- {rid} left the epic since your last approval")
    if delegate and delegate.get("factory"):
        out.append(f"AI Factory: on. Agents split, specify, auto-approve and build children: up to "
                   f"{delegate['max_children']} children of size ≤ {delegate['max_size']} or {delegate['max_hours']} "
                   "hours, then they stop and tell you. Questions are not asked; permissions and the verdict stay yours.")
        if delegate.get("dark"):
            out.append("Dark: on. Agents run without asking you: a shell command runs only if this checkout's Dark "
                       "profile lists it (`orch dark profile list`); anything else is denied and becomes a card. "
                       + release_text(delegate.get("release"), long=True))
    else:
        out.append(f"Delegation: on, up to {delegate['max_children']} children of size ≤ {delegate['max_size']}"
                   if delegate else "Delegation: off")
    return out


def render_verdict(kids, epic: bool = True) -> list[str]:
    """Each open child's criteria and evidence (or one ticket's, `epic=False`), for the terminal, before the
    verdict's typed confirmation."""
    from orch.textsafe import visible
    out = [f"Accepting the epic closes {len(kids)} {'child' if len(kids) == 1 else 'children'}:" if epic
           else "The verdict is given on:"]
    for t in kids:
        out.append(f"- {t.id} {visible(t.title)} · {t.status}")
        _block(out, "Acceptance criteria", t.section("Acceptance criteria"), "    ")
        _block(out, "Verification", t.section("Verification"), "    ")
    return out
