"""The closed harness registry and the launch argv (D-P2-28, §12.4.1; H11c). Pure functions, no I/O.

**The client chooses values, never a command.** A launch is computed here from a harness id of the closed registry
(``claude-code``, ``codex``, ``shell``: the F1 ``actor.id`` values), a mode, a model and the ticket key the host looked
up. The prompt is **one argv element** ("data to the OS, instructions to the harness"); nothing a client sends is ever
parsed by a shell: in the login-shell form the shell runs only the fixed script ``exec "$0" "$@"`` and receives the
program and its arguments as positional parameters. A new harness is a reviewed code change, never configuration.

* ``claude-code``: program ``claude``, ``claude [--model M] <prompt>``; the prompt is the codex text (the
  ``/orch:<mode>`` skills do not exist yet).
* ``codex``: program ``codex``, ``codex [--model M] <prompt>``, prompt
  ``Run orch status, then <mode text> ticket KEY. Follow AGENTS.orch.md.``
* ``shell``: ``$SHELL -l``, no prompt.

Mode ``context``: ``Run orch show KEY --section current_state, then wait for my instructions.`` Without a mode (a
person-run harness, ``term.open``) the harness starts interactive, with no prompt argument.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from orch.modelchoices import MODEL_CHOICES, MODEL_LABELS
from orch.runner.config import RunnerConfig

from .errors import HostError

__all__ = [
    "HARNESSES",
    "LOGIN_EXEC",
    "MODES",
    "MODEL",
    "MODEL_CHOICES",
    "MODEL_LABELS",
    "PROGRAMS",
    "TERM_HARNESSES",
    "CLAUDE_AGENT_FLAGS",
    "MCP_FLAG",
    "MCP_PLACEHOLDER",
    "RULES_FLAG",
    "RULES_PLACEHOLDER",
    "SESSION_FLAG",
    "SESSION_PLACEHOLDER",
    "DISALLOW_DELEGATE",
    "check_model",
    "display",
    "launch_argv",
    "prompt",
]

HARNESSES = ("claude-code", "codex")
TERM_HARNESSES = (*HARNESSES, "shell")
PROGRAMS = {"claude-code": "claude", "codex": "codex"}
MODES = ("refine", "work", "fix", "continue", "context", "task", "followup")
#: what the agent is asked to do in each mode (``Run orch status, then <text> ticket KEY.``)
MODE_TEXT = {"refine": "refine", "work": "work on", "fix": "fix the requested changes on", "continue": "continue"}
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,63}")
#: the only script the login shell ever runs: the program and its arguments arrive as "$0" "$@"
LOGIN_EXEC = 'exec "$0" "$@"'
#: the line the preview shows for a login-shell launch (D-P2-28)
LOGIN_NOTICE = (
    "the program is found and started through your login shell; your shell profile runs first and can change what runs"
)
#: flags of every Claude Code agent launch (see :func:`launch_argv`)
CLAUDE_AGENT_FLAGS = ("--setting-sources", "user", "--strict-mcp-config")
#: the runner replaces the placeholder with the path of the rules file it writes for the run (orch.runner.rules)
RULES_FLAG = "--append-system-prompt-file"
RULES_PLACEHOLDER = "@orch-rules"
#: the runner replaces this placeholder with the path of the MCP config file it writes (orch.runner.mcpconfig)
MCP_FLAG = "--mcp-config"
MCP_PLACEHOLDER = "@orch-mcp"
#: Claude Code gets a session id the runner derives from the run id: the transcript file is then known
SESSION_FLAG = "--session-id"
SESSION_PLACEHOLDER = "@orch-session-id"
#: P2-3: a Claude Code session that may not delegate. One element with ``=``: a variadic flag would swallow the prompt
DISALLOW_DELEGATE = "--disallowedTools=Agent,Task"
KEY = re.compile(r"(?:[A-Z][A-Z0-9]{0,15}-)?[0-9]{1,9}")


def check_model(field: str, value: Any, harness: str | None = None) -> str | None:
    """``None`` when absent or valid, else the ``launch.invalid`` message naming the field. With ``harness`` the name
    must also be one of that harness's closed list (:data:`MODEL_CHOICES`): no free text reaches an argv."""
    if value is None:
        return None
    if not isinstance(value, str) or not MODEL.fullmatch(value):
        return f"{field} is not a model name (letters, digits and ._:[]/-, at most 64, never starting with -)"
    if harness is not None and value not in MODEL_CHOICES.get(harness, ()):
        return f"{field} is not a model orch offers for {harness}"
    return None


def task_prompt(key: str, task: dict[str, Any] | None) -> str:
    """P2-3 mode ``task``: a delegated child of a parent session (one argv element)."""
    if not task or not task.get("session"):
        raise HostError("launch.invalid", "mode task needs a parent run")
    title = str(task.get("title") or "").strip() or "Task"
    brief = str(task.get("brief") or "").strip() or "(none)"
    expected = str(task.get("expected") or "").strip() or "(not specified)"
    return (
        f"Run orch status. You are a delegated task of session {task['session']} on ticket {key}. Task: {title}. "
        f"Brief: {brief} Expected output: {expected} Ask the parent, not the person; log your result with orch log, "
        "then stop."
    )


def prompt(
    mode: str | None, key: str | None, instructions: str | None = None, task: dict[str, Any] | None = None
) -> str | None:
    """The prompt argument, one element; ``None`` without a mode."""
    if mode is None:
        return None
    if key is None or not KEY.fullmatch(key):
        raise HostError("launch.invalid", "a mode needs a ticket")
    if mode == "followup":
        fu = task.get("followup") if isinstance(task, dict) else None
        if isinstance(fu, str) and re.fullmatch(r"sc_[0-7][0-9A-HJKMNP-TV-Z]{25}", fu):
            return (
                f"Run orch status. You were started by schedule {fu} on ticket {key}. "
                f"Run orch schedule show {fu}, then orch show {key}. "
                "Claim the ticket and follow the schedule brief."
            )
        if not isinstance(fu, str) or not re.fullmatch(r"fu_[0-7][0-9A-HJKMNP-TV-Z]{25}", fu):
            raise HostError("launch.invalid", "mode followup needs a valid follow-up id")
        from orch.instructions.harness import AGENTS_FILE

        return (
            f"Run orch status. You were started by follow-up {fu} on ticket {key}. "
            f"Run orch follow-up show {fu}, then orch show {key}, then claim the ticket and continue the work. "
            f"Follow {instructions or AGENTS_FILE}."
        )
    if mode == "task":
        return task_prompt(key, task)
    if mode == "context":
        return f"Run orch show {key} --section current_state, then wait for my instructions."
    if mode not in MODE_TEXT:
        raise HostError("launch.invalid", "mode is not one of refine, work, fix, continue, context")
    from orch.instructions.harness import AGENTS_FILE

    return f"Run orch status, then {MODE_TEXT[mode]} ticket {key}. Follow {instructions or AGENTS_FILE}."


def launch_argv(
    harness: str,
    mode: str | None,
    model: str | None,
    key: str | None,
    config: RunnerConfig,
    instructions: str | None = None,
    workdir: bool = False,
    task: dict[str, Any] | None = None,
    delegate: bool | None = None,
) -> tuple[list[str], bool, str | None]:
    """``(argv, login_shell, program)``. ``shell`` is ``["$SHELL", "-l"]`` (the runner substitutes its own ``SHELL``);
    a harness is ``[$SHELL, "-l", "-c", LOGIN_EXEC, program, …]``, or with ``login_shell: false`` the absolute program
    from ``runner.json`` (none: ``harness.not_found``). ``program`` is that absolute path, else ``None``."""
    if harness == "shell":
        if mode is not None or model is not None:
            raise HostError("launch.invalid", "a shell takes no mode and no model")
        return ["$SHELL", "-l"], False, None
    if harness not in HARNESSES:
        raise HostError("launch.invalid", "harness is not one of claude-code, codex, shell")
    bad = check_model("model", model, harness)
    if bad:
        raise HostError("launch.invalid", bad)
    rest: list[str] = []
    if model is not None:
        rest += ["--model", model]
    if harness == "claude-code":
        rest += [SESSION_FLAG, SESSION_PLACEHOLDER]
        if delegate is False:  # P2-3 policy: the main session may not start sub-agents
            rest.append(DISALLOW_DELEGATE)
    if harness == "claude-code" and mode is not None and workdir:
        # ONLY an agent in a ticket work folder (<project>/.orch-work/<KEY>, `workdir`: the folders the runner
        # pre-trusts in Claude Code) reads no project or local settings and no .mcp.json (hooks, permissions, env and
        # MCP servers a collaborator could commit there). The user's own settings and the installed plugins (orch-core's
        # SessionStart hook) still load. Side effects, checked against Claude Code 2.1.309: the project's and its
        # ancestors' CLAUDE.md files, project skills, commands and subagents, project permission allowlists and every
        # MCP server are not loaded either. So orch's rules arrive as an appended system prompt file that the runner
        # writes (`@orch-rules`, see orch.runner.rules). Company MCP servers arrive the same way, as a config file the
        # runner writes (`@orch-mcp`, see orch.runner.mcpconfig; empty without any); the agent's own project
        # .mcp.json still never loads. Worktree, repo and
        # workspace-root launches keep Claude Code's normal behaviour (their project config is the person's own) and
        # get no pre-trust, so Claude Code's own prompts stay. After --model, which the runner expects first.
        rest += [*CLAUDE_AGENT_FLAGS, MCP_FLAG, MCP_PLACEHOLDER, RULES_FLAG, RULES_PLACEHOLDER]
    text = prompt(mode, key, instructions, task)
    if text is not None:
        rest.append(text)
    if config.login_shell:
        return ["$SHELL", "-l", "-c", LOGIN_EXEC, PROGRAMS[harness], *rest], True, None
    program = config.programs.get(harness)
    if not program:
        raise HostError("harness.not_found", f"runner.json names no program for {harness} (login_shell is off)")
    return [program, *rest], False, program


def display(argv: list[str]) -> str:
    """The POSIX-quoted rendering of ``argv``, for display only (``$SHELL`` stays as written)."""
    return " ".join(a if a == "$SHELL" else shlex.quote(a) for a in argv)
