"""Start agent (spec §7): which mode and prompt to suggest for a ticket, and the exact argv.

"Agents only start when you start them": nothing here runs anything; `launch.start` does, and only
from the dashboard POST. Only `{key}` (validated) and `{pr}` (validated) are substituted into a
prompt; ticket text never enters a prompt or command.
"""
from __future__ import annotations

import os
import re
import shlex
import sys

from orch.config.load import DEFAULTS
from orch.core.events import read_events
from orch.core.gates import GATE_SECTIONS, changes_pending
from orch.dashboard.data.steps import when, your_move
from orch.errors import ValidationError

MODES = ("refine", "work", "fix-checks", "continue")
EPIC_MODES = ("refine", "continue")  # an epic has no plan, tasks or claim of its own
MODE_LABELS = {"refine": "Refine", "work": "Work on ticket", "fix-checks": "Fix failing checks",
               "continue": "Continue after feedback"}
HARNESS_LABELS = {"claude": "Claude Code", "copilot": "Copilot CLI", "codex": "Codex"}
STALE_WARNING = "Starting releases the stale claim first"

KEY_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")  # always used with fullmatch: no trailing newline slips through
_PR_RE = re.compile(r"(?:#?\d+|https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%+/-]*)?)")
IGNORED_WARNING = "terminal settings in orchestrator/config.json are ignored — use ~/.config/orch/launch.json"


def _agents_cfg(ws) -> dict:
    cfg = ws.config.get("agents")
    return cfg if isinstance(cfg, dict) else DEFAULTS["agents"]


def _settings(settings):
    from orch.dashboard import launch

    return settings if settings is not None else launch.load_settings()


def harnesses(ws, settings=None) -> dict[str, list[str]]:
    """The known harnesses and their argv templates: built-in, plus the user's launch.json. Never
    from the workspace config (an agent could plant a launcher there)."""
    return dict(_settings(settings)["harnesses"])


def default_harness(ws, settings=None) -> str | None:
    """The workspace's agents.default_harness when it names a known harness, else the user's."""
    settings = _settings(settings)
    known = harnesses(ws, settings)
    for name in (_agents_cfg(ws).get("default_harness"), settings.get("default_harness")):
        if isinstance(name, str) and name in known:
            return name
    return next(iter(known), None)


def launch_warnings(ws, settings=None) -> list[str]:
    """Sentences for the Workspace page: a broken launch.json, or launch settings left in the
    workspace config (ignored)."""
    out = []
    error = _settings(settings).get("error")
    if error:
        out.append(error)
    dashboard = ws.config.get("dashboard") if isinstance(ws.config.get("dashboard"), dict) else {}
    agents = ws.config.get("agents") if isinstance(ws.config.get("agents"), dict) else {}
    if "terminal" in dashboard or "terminal_command" in dashboard or "harnesses" in agents:
        out.append(IGNORED_WARNING)
    for mode in bad_prompts(ws):
        out.append(f"agents.prompts.{mode} in orchestrator/config.json is ignored: it must contain {{key}}, "
                   f"must not start with '-' and must not hold control characters; using the built-in {mode} prompt")
    return out


_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")  # every control character but a newline


def prompt_ok(template) -> bool:
    """A workspace prompt template is used only when it contains {key}, holds no control character
    but a newline, and the rendered prompt does not start with "-" (it must never turn into a
    harness flag such as --dangerously-skip-permissions)."""
    if not isinstance(template, str) or "{key}" not in template or _CONTROL.search(template):
        return False
    return not template.replace("{key}", "K-1").replace("{pr}", "1").strip().startswith("-")


def bad_prompts(ws) -> list[str]:
    """Modes whose workspace prompt template is set but refused by prompt_ok (they use the default)."""
    prompts = _agents_cfg(ws).get("prompts")
    if not isinstance(prompts, dict):
        return []
    return [m for m in MODES if m in prompts and prompts[m] != DEFAULTS["agents"]["prompts"][m]
            and not prompt_ok(prompts[m])]


def _prompt_template(ws, mode: str) -> str:
    prompts = _agents_cfg(ws).get("prompts")
    value = prompts.get(mode) if isinstance(prompts, dict) else None
    return value if prompt_ok(value) else DEFAULTS["agents"]["prompts"][mode]


def valid_pr(pr) -> bool:
    return isinstance(pr, (str, int)) and not isinstance(pr, bool) and bool(_PR_RE.fullmatch(str(pr)))


def build(ws, key: str, mode: str, harness: str, *, pr=None, settings=None) -> tuple[str, list[str], str]:
    """(prompt, argv, display_command) for starting `harness` on ticket `key` in `mode`.

    Raises ValidationError for a key not matching ^[A-Z][A-Z0-9]*-\\d+$, an unknown mode or
    harness, or a prompt that needs `{pr}` without a valid PR URL or number."""
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValidationError(f"not a ticket key: {key!r}")
    if mode not in MODES:
        raise ValidationError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
    known = harnesses(ws, settings)
    if not isinstance(harness, str) or harness not in known:
        raise ValidationError(f"unknown harness {harness!r}; configured: {', '.join(known) or 'none'}")
    template = _prompt_template(ws, mode)
    if "{pr}" in template or mode == "fix-checks":
        if not valid_pr(pr):
            raise ValidationError("this mode needs a linked PR")
    prompt = template.replace("{key}", key).replace("{pr}", str(pr) if pr is not None else "")
    if prompt.strip().startswith("-") or _CONTROL.search(prompt):  # belt and braces: never a flag
        raise ValidationError(f"the {mode} prompt must not start with '-' or hold control characters")
    argv = [part.replace("{prompt}", prompt) for part in known[harness]]
    return prompt, argv, f"cd {shlex.quote(str(ws.root))} && {shlex.join(argv)}"


def _pr(ticket):
    prs = ticket.meta.get("prs")
    for item in prs if isinstance(prs, list) else []:
        url = item.get("url") if isinstance(item, dict) else None
        if valid_pr(url):
            return url
    return None


def _answered_since_agent(ticket, events) -> bool:
    last_agent = max((at for e in events or [] if str(getattr(e, "actor", "")).startswith("agent:")
                      and (at := when(getattr(e, "at", None)))), default=None)
    questions = ticket.meta.get("questions")
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict) or q.get("answer") in (None, "", []):
            continue
        at = when(q.get("answered"))
        if at and (last_agent is None or at > last_agent):
            return True
    return False


def _mode(ticket, events) -> str:
    if any(changes_pending(ticket, g) for g in GATE_SECTIONS) or _answered_since_agent(ticket, events):
        return "continue"
    return "refine" if ticket.status == "backlog" else "work"


def _waiting_reason(move: dict) -> str | None:
    if move.get("kind") != "human":
        return None
    action = move.get("action") or {}
    kind = action.get("kind")
    if kind == "answer":
        return "Waiting for your answer"
    if kind == "verdict":
        return "Waiting for your verdict"
    gate = action.get("gate")
    if kind == "approve" and gate:
        return f"Waiting for your {gate} approval"
    if kind == "move" and action.get("to"):
        # a changed gate that cannot be re-approved in this status: the human's move is a move
        return f"Waiting for you: move it back to {action['to']}"
    if kind == "hint" and move.get("text"):
        return f"Waiting for you: {move['text']}"
    return "Waiting for you"


def suggest(ws, ticket, *, needs_items, rows=None, now=None, events=None, settings=None, failing_prs=(),
            brief: bool = False, request=None) -> dict | None:
    """What the Start agent box shows for `ticket`, or None when the ticket is done (or its id
    cannot be started on).

    `needs_items`: query.needs_you() items (other tickets' are ignored); `rows`: agents.agent_rows()
    (computed with `now` when not given); `events`: this ticket's events (read when not given).
    `settings`: launch.load_settings() (read when not given). Callers that render many tickets
    (Today) compute needs_items, rows and settings once and pass them. `failing_prs`: PR URLs with failing
    checks that a reviews addon links to this ticket; the first becomes {pr} and turns work into fix-checks.

    Returns {key, mode, harness, prompt, command, disabled, disabled_role, warning, pr, terminal,
    terminal_label, launch_path, launcher, modes, harnesses, options}; `disabled` is "Waiting for your
    <gate> approval", "Waiting for you: move it back to <status>" (or ": <hint text>") for a changed
    gate that cannot be re-approved in this status, "Waiting for your answer", "Waiting for your
    verdict" or "<harness> is working on it" (fresh claim), else None; a stale claim keeps it enabled with `warning` = STALE_WARNING. `options`
    holds prompt, command and launcher (the substituted custom launcher, else None) for every
    harness x mode, for the live preview. Terminal and harness argv come from launch.json only.
    `brief=True` stops before any prompt is built and returns only {key, harness, disabled, disabled_role, warning}:
    what Today's In flight cards need to offer the one shared Start agent panel. `request`: the page's request; Open in
    Mission Control is offered only to a local one (terminals.local_request), never without it."""
    from orch.dashboard import launch, terminals
    from orch.dashboard.data.agents import agent_rows

    if ticket.status == "done" or not KEY_RE.fullmatch(ticket.id):
        return None
    settings = _settings(settings)
    harness = default_harness(ws, settings)
    if harness is None:
        return None
    if events is None:
        events = read_events(ws, ticket.id)
    if rows is None:
        rows = agent_rows(ws, now=now, needs=needs_items)
    skip = tuple(ws.config["gates"]["plan_skip_sizes"])
    own = [i for i in needs_items or [] if isinstance(i, dict) and str(i.get("ticket", "")).upper() == ticket.id.upper()]
    disabled = _waiting_reason(your_move(ticket, own, plan_skip_sizes=skip))
    role = "you" if disabled else None
    warning = None
    claim = ticket.meta.get("claim") if isinstance(ticket.meta.get("claim"), dict) else {}
    if claim.get("session"):
        row = next((r for r in rows or [] if r.ticket == ticket.id), None)
        if row is not None and row.status == "stale":
            warning = STALE_WARNING
        elif disabled is None:
            disabled, role = f"{claim.get('harness') or 'an agent'} is working on it", "info"
    if brief:
        return {"key": ticket.id, "harness": harness, "disabled": disabled, "disabled_role": role, "warning": warning}
    failing = [u for u in failing_prs or () if valid_pr(u)]
    pr = failing[0] if failing else _pr(ticket)
    terminal = launch.choose(os.environ, settings["terminal"], sys.platform)
    options = []
    epic = ticket.meta.get("type") == "epic"
    for h in harnesses(ws, settings):
        for m in EPIC_MODES if epic else MODES:  # an epic is refined, never worked: its children are
            try:  # a mode whose prompt needs a PR is left out while none is linked
                p, argv, c = build(ws, ticket.id, m, h, pr=pr, settings=settings)
            except ValidationError:
                continue
            options.append({"harness": h, "mode": m, "prompt": p, "command": c,
                            "launcher": launch.preview(ws, ticket.id, argv, terminal=terminal, settings=settings)})
    modes = [m for m in MODES if any(o["mode"] == m for o in options)]
    mode = _mode(ticket, events)
    if epic and mode == "work":
        mode = "refine"
    if failing and mode == "work" and "fix-checks" in modes:
        mode = "fix-checks"
    if mode not in modes:
        mode = "refine" if ticket.status == "backlog" and "refine" in modes else (modes[0] if modes else None)
    if mode is None:
        return None
    chosen = next(o for o in options if o["harness"] == harness and o["mode"] == mode)
    prompt, command = chosen["prompt"], chosen["command"]
    here = request is not None and terminals.enabled(ws, request)  # Terminals (#40): on, and a local request
    return {
        "key": ticket.id, "mode": mode, "harness": harness, "prompt": prompt, "command": command,
        "disabled": disabled, "disabled_role": role, "warning": warning, "pr": pr,
        "terminal": terminal, "terminal_label": launch.LABELS.get(terminal, terminal),
        # a second button that opens it in Mission Control's own Terminals (issue #40), while tmux is installed
        "mission_control": terminal not in ("tmux", "none") and here,
        # the addon's "Start agents in Mission Control by default": its button comes first, as the primary
        "mission_control_first": here and bool(terminals.settings(ws.root)["open_here"]),
        "launcher": chosen["launcher"], "launch_path": settings["path"],
        "modes": [{"value": m, "label": "Refine epic" if epic and m == "refine" else MODE_LABELS[m]} for m in modes],
        "harnesses": [{"value": h, "label": HARNESS_LABELS.get(h, h)} for h in harnesses(ws, settings)],
        "options": options,
    }

