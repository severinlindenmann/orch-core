"""Open a terminal that runs an agent command (spec §7, "Open in terminal").

Only an explicit dashboard POST gets here. Every launcher is an argv list (never `shell=True`);
the agent command reaches a shell only through `shlex.join` (cmux's --command and the generated
`.command` script), and the values in it were validated by `agent_start.build`.

What gets launched is set per user, never per workspace: agents can write the workspace's
orchestrator/config.json, so the terminal and the harness argv come only from the user file
`launch.json` in the orch config directory ($ORCH_STATE_DIR, else $XDG_CONFIG_HOME/orch, else
~/.config/orch). All keys are optional:

    {
      "terminal": "auto",            # auto | cmux | terminal | iterm | ghostty | linux | windows | tmux | custom | none
      "terminal_command": [],        # argv for "custom", see placeholders below
      "harnesses": {"claude": ["claude", "{prompt}"]},   # merged over the built-in harnesses
      "default_harness": "claude"
    }

`terminal_command` placeholders, replaced inside each element as is: {cwd} (workspace root),
{command} (the agent command, already shlex.join-ed into one shell string), {script} (the
generated 0700 .command script), {name} (the ticket key). For an element that a shell will parse
(e.g. `["sh", "-c", "cd {cwd_q} && {command}"]`) use the shlex.quote-d forms {cwd_q},
{script_q}, {name_q}. A missing file means the built-in defaults; a broken one means the
defaults plus a warning on the Workspace page.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess as _subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from orch.errors import UsageError

# launch's own handle on Popen: tests patch `launch.subprocess.Popen` without replacing the
# subprocess module's Popen that git and everything else use.
subprocess = SimpleNamespace(Popen=_subprocess.Popen, DEVNULL=_subprocess.DEVNULL)
which = shutil.which  # module-level so tests can stand in for binaries a CI runner lacks

TERMINALS = ("auto", "cmux", "terminal", "iterm", "ghostty", "linux", "windows", "tmux", "custom", "none")
LABELS = {"cmux": "cmux", "terminal": "Terminal", "iterm": "iTerm", "ghostty": "Ghostty",
          "linux": "the terminal", "windows": "Windows Terminal", "tmux": "Mission Control",
          "custom": "custom launcher", "none": "off"}
_PROGRAMS = {"iTerm.app": "iterm", "ghostty": "ghostty", "Apple_Terminal": "terminal"}
_LAUNCHER_BIN = {"cmux": "cmux", "terminal": "open", "iterm": "open", "ghostty": "open",
                 "linux": "x-terminal-emulator", "windows": "wt.exe", "tmux": "tmux"}
HARNESS_NAME = re.compile(r"[a-z][a-z0-9_-]*")
DEFAULT_HARNESSES = {"claude": ["claude", "{prompt}"], "copilot": ["copilot", "-i", "{prompt}"],
                     "codex": ["codex", "{prompt}"]}
SETTINGS_KEYS = ("terminal", "terminal_command", "harnesses", "default_harness", "factory_command")
# What the AI Factory runner starts for one child (docs/factory.md): {session} is the id the runner generated and binds
# (the harness must start its session under exactly that id), {prompt} the child's work prompt. Per user only, like
# every launch setting.
DEFAULT_FACTORY_COMMAND = ["claude", "--session-id", "{session}", "{prompt}"]
# An argument that would hand the agent permissions itself: the permission hook stays the only gate.
_SELF_GRANT = re.compile(r"dangerously|bypass|allowed-?tools|permission-prompt-tool|--settings|yolo|--trust-all|--full-auto"
                         r"|--auto-approve|--yes\b", re.I)


def factory_command_error(argv) -> str | None:
    """Why `argv` cannot be the runner's launch command, or None."""
    if not _argv_list(argv):
        return "factory_command must be a non-empty list of strings"
    if not any("{session}" in a for a in argv) or not any("{prompt}" in a for a in argv):
        return "factory_command must contain {session} and {prompt}"
    if any(_SELF_GRANT.search(a) for a in argv):
        return "factory_command must not grant the agent permissions itself: the permission hook is the gate"
    return None


def config_dir() -> Path:
    base = os.environ.get("ORCH_STATE_DIR")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "orch"


def config_path() -> Path:
    return config_dir() / "launch.json"


def _defaults() -> dict:
    return {"terminal": "auto", "terminal_command": [], "default_harness": "claude",
            "harnesses": {k: list(v) for k, v in DEFAULT_HARNESSES.items()},
            "factory_command": list(DEFAULT_FACTORY_COMMAND), "error": None, "path": str(config_path())}


def _argv_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(a, str) and a for a in value)


def load_settings() -> dict:
    """The user's launch settings: {terminal, terminal_command, harnesses, default_harness,
    error, path}. `error` is a sentence for the Workspace page when the file is broken; the rest
    are then the built-in defaults (nothing from a broken file is used). Never raises."""
    path = config_path()
    try:
        return _load_settings(path)
    except Exception as e:  # any shape we did not foresee: defaults plus a warning, never a crash
        return {**_defaults(), "error": f"{path} is ignored: it could not be read ({type(e).__name__}); using the defaults"}


def _load_settings(path: Path) -> dict:
    out = _defaults()

    def broken(why: str) -> dict:
        return {**_defaults(), "error": f"{path} is ignored: {why}; using the defaults"}

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return out
    except OSError as e:
        return {**out, "error": f"{path} cannot be read ({e.strerror or e}); using the defaults"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return broken("it is not UTF-8 text")
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        return broken(f"invalid JSON ({e})")
    if not isinstance(data, dict):
        return broken("it must be a JSON object")
    unknown = sorted(str(k) for k in set(data) - set(SETTINGS_KEYS))
    if unknown:
        return broken(f"unknown key {', '.join(unknown)}")
    terminal = data.get("terminal", "auto")
    if not isinstance(terminal, str) or terminal not in TERMINALS:
        return broken(f"terminal must be one of {', '.join(TERMINALS)}")
    command = data.get("terminal_command", [])
    if not isinstance(command, list) or not all(isinstance(a, str) for a in command):
        return broken("terminal_command must be a list of strings")
    harnesses = data.get("harnesses", {})
    if not isinstance(harnesses, dict) or not all(
            isinstance(k, str) and HARNESS_NAME.fullmatch(k) and _argv_list(v) and any("{prompt}" in a for a in v)
            for k, v in harnesses.items()):
        return broken("harnesses must map lower-case names to non-empty lists of strings with a {prompt} element")
    merged = {**out["harnesses"], **{k: list(v) for k, v in harnesses.items()}}
    default = data.get("default_harness", "claude")
    if not isinstance(default, str) or default not in merged:
        return broken("default_harness must name a known harness")
    factory_command = data.get("factory_command", DEFAULT_FACTORY_COMMAND)
    if factory_command_error(factory_command):
        return broken(factory_command_error(factory_command))
    return {**out, "terminal": terminal, "terminal_command": list(command), "harnesses": merged,
            "default_harness": default, "factory_command": list(factory_command)}


def choose(env: Mapping[str, str], configured: str, platform: str) -> str:
    """The terminal to open: a configured value other than "auto" wins; "auto" looks at the
    environment `orch serve` was started in, then at the platform."""
    if configured and configured != "auto":
        return configured
    if any(k.startswith("CMUX_") for k in env):
        return "cmux"
    program = _PROGRAMS.get(env.get("TERM_PROGRAM", ""))
    if program:
        return program
    if platform.startswith("linux"):
        return "linux"
    if platform.startswith("win"):
        return "windows"
    return "terminal"


def _needs_script(terminal: str, custom) -> bool:
    if terminal in ("cmux", "windows", "tmux"):
        return False
    if terminal == "custom":
        return any("{script" in str(part) for part in custom or [])
    return True


def tmux_arg(value: str) -> str:
    """tmux reads an argument that ends in ';' as the end of a command (and drops the ';'); '\\;' keeps it."""
    return value[:-1] + "\\;" if value.endswith(";") else value


def argv_for(terminal: str, *, cwd: str, command_argv: list[str], name: str, script_path: str | None,
             custom: list[str]) -> list[str]:
    """The launcher's argv. No element is ever run through a shell by us."""
    if terminal == "cmux":
        return ["cmux", "new-workspace", "--name", name, "--cwd", cwd, "--command", shlex.join(command_argv),
                "--focus", "true"]
    if terminal == "terminal":
        return ["open", "-a", "Terminal", script_path]
    if terminal == "iterm":
        return ["open", "-a", "iTerm", script_path]
    if terminal == "ghostty":
        return ["open", "-na", "Ghostty", "--args", "-e", script_path]
    if terminal == "linux":
        return ["x-terminal-emulator", "-e", script_path]
    if terminal == "windows":
        return ["wt.exe", "-d", cwd, *command_argv]
    if terminal == "tmux":  # orch's own server, detached: shown in Mission Control's Terminals (issue #40)
        # `env -u TMUX`: inside the session, a plain `tmux` must not reach orch's server (and the other sessions)
        command = shlex.join(["env", "-u", "TMUX", "-u", "TMUX_PANE", *command_argv])
        return ["tmux", "-L", "orch", "new-session", "-d", "-s", name, "-c", tmux_arg(cwd), "-x", "160", "-y", "45",
                tmux_arg(command)]
    if terminal == "custom":
        script = script_path or ""
        values = {"{cwd}": cwd, "{command}": shlex.join(command_argv), "{script}": script, "{name}": name,
                  "{cwd_q}": shlex.quote(cwd), "{script_q}": shlex.quote(script), "{name_q}": shlex.quote(name)}
        pattern = re.compile("|".join(re.escape(k) for k in sorted(values, key=len, reverse=True)))
        return [pattern.sub(lambda m: values[m.group(0)], str(part)) for part in custom or []]
    raise UsageError(f"unknown terminal {terminal!r}")


def preview(ws, key: str, command_argv: list[str], *, terminal: str, settings: dict) -> str | None:
    """The launcher as it will run, for the Start agent box (custom only; the others are fixed)."""
    if terminal != "custom" or not settings.get("terminal_command"):
        return None
    script = str(ws.state_dir / "run" / f"{key}-<time>.command")
    return shlex.join(argv_for("custom", cwd=str(ws.root), command_argv=command_argv, name=key,
                               script_path=script, custom=settings["terminal_command"]))


def preflight(terminal: str, settings: dict) -> None:
    """Cheap checks before anything changes (e.g. before a stale claim is released)."""
    if terminal == "none":
        raise UsageError("Open in terminal is turned off")
    if terminal not in TERMINALS or terminal == "auto":
        raise UsageError(f"unknown terminal {terminal!r}")
    custom = settings.get("terminal_command") or []
    if terminal == "custom" and not custom:
        raise UsageError(f"terminal is custom but terminal_command is empty in {config_path()}")
    binary = custom[0] if terminal == "custom" else _LAUNCHER_BIN[terminal]
    if which(binary) is None:
        raise UsageError(f"{binary} was not found; set terminal in {config_path()}")


def _write_script(ws, key: str, argv: list[str]) -> str:
    run = ws.state_dir / "run"
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = run / f"{key}-{time.time_ns()}.command"
    text = f"#!/bin/sh\ncd {shlex.quote(str(ws.root))} && exec {shlex.join(argv)}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o700)  # the umask may have taken bits away; the script must stay runnable
    return str(path)


def start(ws, key: str, argv: list[str], *, terminal: str, name: str, harness: str | None = None,
          settings: dict | None = None) -> str:
    """Open `terminal` running `argv` in the workspace root, detached; returns the flash message
    "Opened <harness> in <terminal> for <key>". `argv` comes from `agent_start.build`."""
    settings = settings if settings is not None else load_settings()
    preflight(terminal, settings)
    custom = list(settings.get("terminal_command") or [])
    script = _write_script(ws, key, argv) if _needs_script(terminal, custom) else None
    launcher = argv_for(terminal, cwd=str(ws.root), command_argv=argv, name=name, script_path=script, custom=custom)
    try:
        proc = subprocess.Popen(launcher, start_new_session=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        # tmux returns as soon as the detached session exists: wait for that, so the Terminals page has it
        if terminal == "tmux" and proc.wait(timeout=10) != 0:
            raise UsageError(f"tmux could not start a session named {name}")
    except OSError as e:
        raise UsageError(f"could not open {LABELS.get(terminal, terminal)}: {e.strerror or e}") from e
    except _subprocess.TimeoutExpired as e:
        raise UsageError("tmux did not answer within 10 seconds") from e
    return f"Opened {harness or argv[0]} in {LABELS.get(terminal, terminal)} for {key}"
