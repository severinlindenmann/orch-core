"""The ticket-usage addon's status line recorder (#166): the `usage-recorder` doctor check and the human-run
`orch addon setup ticket-usage`, which copies the script and adds a status line to the user-global Claude settings."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

ADDON = "ticket-usage"
SETUP_CMD = "orch addon setup ticket-usage"
DEFAULT_LOG = "~/.claude/orch-usage/limits.jsonl"
MARK = "orch-usage/statusline.sh"  # what a status line that runs the recorder contains
_MAX_SCRIPT = 256 * 1024


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def settings_path() -> Path:
    return claude_dir() / "settings.json"


def script_path() -> Path:
    return claude_dir() / "orch-usage" / "statusline.sh"


def source_script() -> Path | None:
    from orch.addons.discovery import find
    found = find(ADDON)
    path = found.folder / "recorder" / "statusline.sh" if found else None
    return path if path and path.is_file() else None


def script_command() -> str:
    """The recorder as a status line command: `~/…` under HOME (as the README writes it), else a quoted path."""
    path = script_path()
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return shlex.quote(str(path))


def add_line() -> str:
    """The one line an existing status line script adds, after it has read stdin into $input."""
    return f"printf '%s' \"$input\" | {script_command()} >/dev/null"


def read_settings(path: Path | None = None) -> tuple[dict | None, str | None]:
    """(settings, None), ({}, None) when the file is missing, or (None, why) when it cannot be used."""
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return None, f"{path} cannot be read as JSON ({type(e).__name__})"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    return data, None


def status_command(settings: dict) -> str | None:
    """The status line's command, "" for a status line of another shape, None when there is none."""
    if "statusLine" not in settings:
        return None
    line = settings["statusLine"]
    cmd = line.get("command") if isinstance(line, dict) else None
    return cmd if isinstance(cmd, str) else ""


def calls_recorder(command: str | None) -> bool:
    """The command names the recorder, or is a script file that does."""
    if not command:
        return False
    if MARK in command:
        return True
    try:
        first = Path(shlex.split(command)[0]).expanduser()
        if first.is_file() and first.stat().st_size <= _MAX_SCRIPT:
            return MARK in first.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, IndexError):
        pass
    return False


def log_path(root) -> Path:
    """The workspace's Limits log setting (Workspace & addons), else the default."""
    from orch.addons.userfiles import workspace_addons
    value = workspace_addons(root).get(ADDON, {}).get("config", {}).get("limits_log")
    return Path(value if isinstance(value, str) and value.strip() else DEFAULT_LOG).expanduser()


def check(ws):
    """The doctor check while ticket-usage is enabled here, else None."""
    from orch.addons.userfiles import workspace_addons
    from orch.onboarding import Check
    try:
        if workspace_addons(ws.root).get(ADDON, {}).get("enabled") is not True:
            return None
        log = log_path(ws.root)
    except Exception:
        return None
    if log.is_file():
        return Check("usage-recorder", True, f"the limits log exists ({log})")
    settings, problem = read_settings()
    if problem:
        return Check("usage-recorder", False, f"ticket-usage is enabled but the limits recorder cannot be checked: {problem}",
                     f"fix {settings_path()}, then run `{SETUP_CMD}` in your own terminal")
    command = status_command(settings)
    if calls_recorder(command):
        return Check("usage-recorder", True, "your status line runs the limits recorder; limits appear after "
                                             "Claude Code's next reply")
    if command is not None:
        return Check("usage-recorder", False,
                     "ticket-usage is enabled but your status line does not run the limits recorder, so the Usage "
                     "page shows no limits",
                     f"run `{SETUP_CMD}` in your own terminal (it copies the recorder and leaves your status line "
                     f"alone), then add this line to your status line script after it reads stdin into $input: "
                     f"{add_line()}")
    return Check("usage-recorder", False,
                 "ticket-usage is enabled but the limits recorder is not installed, so the Usage page shows no limits; "
                 f"the human runs the fix in their own terminal; it adds a status line to {settings_path()} (every "
                 "Claude Code session on this machine) after a typed confirmation",
                 SETUP_CMD)


@dataclass(frozen=True)
class Plan:
    source: Path
    target: Path
    settings: Path
    copy: bool  # the target is missing, differs or is not executable
    add_status_line: bool
    existing: str | None  # the current status line command, when there is one
    settings_problem: str | None


def plan() -> Plan:
    from orch.errors import UsageError
    source = source_script()
    if source is None:
        raise UsageError(f"the {ADDON} addon's recorder/statusline.sh was not found", hint="reinstall the orch-core plugin")
    target = script_path()
    try:
        same = target.read_bytes() == source.read_bytes() and os.access(target, os.X_OK)
    except OSError:
        same = False
    settings, problem = read_settings()
    existing = status_command(settings) if settings is not None else None
    return Plan(source, target, settings_path(), not same, settings is not None and existing is None, existing, problem)


def plan_text(p: Plan) -> str:
    lines = []
    if p.copy:
        lines.append(f"Copy {p.source}\n  to {p.target} and make it executable.")
    else:
        lines.append(f"{p.target} is up to date.")
    if p.add_status_line:
        lines.append(f'Add "statusLine": {{"type": "command", "command": "{script_command()}"}} to {p.settings}.\n'
                     "  These are your user-global Claude Code settings: every Claude Code session on this machine "
                     "runs it.")
    elif p.settings_problem:
        lines.append(f"Leave {p.settings} alone: {p.settings_problem}.")
    elif calls_recorder(p.existing):
        lines.append("Your status line already runs the recorder.")
    else:
        lines.append(f"Your status line stays as it is ({p.existing or 'not a command'}).")
    if not shutil.which("jq"):
        lines.append("Note: jq is not installed; the recorder needs it (macOS: brew install jq).")
    return "\n".join(lines)


def manual_steps(p: Plan) -> str | None:
    """What the human still does by hand after apply, or None."""
    if p.add_status_line or calls_recorder(p.existing):
        return None
    entry = f'"statusLine": {{"type": "command", "command": "{script_command()}"}}'
    if p.settings_problem:
        return f"Add {entry} to {p.settings} once it is valid JSON."
    return (f"Your status line was not changed. Add this line to your status line script, after it reads stdin "
            f"into $input:\n  {add_line()}")


def apply(p: Plan) -> list[str]:
    """Do what the plan says; returns what changed. Never replaces a status line that is already there."""
    from orch.actor import require_human_terminal
    from orch.core.fsutil import atomic_write_text
    from orch.errors import OrchError
    require_human_terminal("setting up the ticket-usage recorder",
                           hint=f"it changes your user-global Claude settings: run `{SETUP_CMD}` in your own terminal")
    done = []
    if p.copy:
        p.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p.source, p.target)
        p.target.chmod(p.target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        done.append(f"copied the recorder to {p.target}")
    if p.add_status_line:
        settings, problem = read_settings(p.settings)  # read again: it may have changed while the human typed
        if problem or status_command(settings or {}) is not None:
            raise OrchError(f"{p.settings} changed since it was read; nothing was added to it",
                            hint=f"run `{SETUP_CMD}` again")
        settings["statusLine"] = {"type": "command", "command": script_command()}
        real = p.settings.resolve()  # a symlinked settings.json (dotfiles) is written where it points
        mode = stat.S_IMODE(real.stat().st_mode) if real.exists() else None
        atomic_write_text(real, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        if mode is not None:
            real.chmod(mode)
        done.append(f"added the status line to {p.settings}")
    return done
