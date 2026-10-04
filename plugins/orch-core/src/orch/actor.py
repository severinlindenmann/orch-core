"""Who is running this CLI call?

Agents are detected by environment (ORCH_HARNESS, Claude Code's CLAUDECODE / CLAUDE_CODE_* variables, AI_AGENT,
CODEX_SANDBOX, ...) and, since an agent can unset its own environment, by process ancestry: any ancestor process that
is a known agent harness (Claude Code, Copilot CLI, Codex, Gemini CLI, Cursor agent, aider, ...) makes the caller an
agent. Human-only actions additionally need an interactive TTY and a typed confirmation. Every human event records
this process evidence, and `orch check` reports a human event whose evidence names an agent harness.

Known limits: ancestry raises the bar, it proves nothing. A process an agent detaches from its own tree (reparented
to init), or input typed into a real terminal the human opened (e.g. an IDE agent using the integrated terminal), is
not a descendant of the harness and passes these checks (orch's own tmux server, where Mission Control's terminals
run, is treated as a harness for that reason); so does a harness this module does not know, or one that
hides its parent processes (a separate PID namespace). The typed confirmation and the signed approval ledger
(orch.core.ledger) are the real line; `orch check` and the dashboard audit. On POSIX an unreadable process tree counts as an agent (fail closed); on Windows, where it is not
read, only the environment is checked.

The same applies to `orch serve`: it refuses inside an agent harness and without a TTY, and the Claude guard denies
Bash commands that start it, but an agent with a real TTY outside the harness's process tree can still start the
dashboard, read the token it prints and act through it.
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import sys

from orch.core.events import Actor
from orch.errors import HumanOnlyError

_HINT = "ask the human to do this in their own terminal or in the dashboard"


# Environment variables agent harnesses set for the commands they run (checked after ORCH_HARNESS).
_ENV_MARKERS = (
    ("CLAUDECODE", "claude-code"), ("CLAUDE_CODE_SESSION_ID", "claude-code"), ("CLAUDE_CODE_ENTRYPOINT", "claude-code"),
    ("CODEX_SANDBOX", "codex"), ("CODEX_SANDBOX_NETWORK_DISABLED", "codex"), ("GEMINI_CLI", "gemini-cli"),
)
# Process names of agent harnesses: the basename of argv[0], or of the script an interpreter runs.
_HARNESS_NAMES = {
    "claude": "claude-code", "claude-code": "claude-code", "codex": "codex", "copilot": "copilot-cli",
    "github-copilot-cli": "copilot-cli", "gemini": "gemini-cli", "aider": "aider", "cursor-agent": "cursor-agent",
    "opencode": "opencode", "goose": "goose", "amp": "amp", "qwen": "qwen-code", "crush": "crush", "cline": "cline",
    "kiro-cli": "kiro", "q": "amazon-q", "auggie": "auggie", "droid": "factory-droid",
}
# Package paths that give a harness away when it runs as `node …/cli.js` and similar.
_PACKAGE_MARKERS = (
    ("@anthropic-ai/claude-code", "claude-code"), ("@openai/codex", "codex"), ("@github/copilot", "copilot-cli"),
    ("@google/gemini-cli", "gemini-cli"), ("opencode-ai", "opencode"), ("@sourcegraph/amp", "amp"),
    ("@qwen-code/qwen-code", "qwen-code"), ("@augmentcode/auggie", "auggie"),
)
_INTERPRETER = re.compile(r"^(?:node|nodejs|bun|deno|ruby|npx|pipx|uvx|python(?:[0-9.]*)?|pypy[0-9.]*)$")
_SCRIPT_EXT = re.compile(r"\.(?:js|mjs|cjs|ts|py|exe)$")
_MAX_DEPTH = 64


def _base(word: str) -> str:
    return _SCRIPT_EXT.sub("", re.split(r"[/\\]", word.lstrip("-"))[-1]).lower()


# Mission Control's terminals (#40) run in orch's own tmux server (`tmux -L orch`). Its panes are children of that
# server, not of whoever asked it for a window, and have a real TTY; so the server itself counts as a harness: no
# process in it passes for the human's own terminal. Its command line (`tmux … -L orch …`, `-S …/orch`), or the
# `tmux: server (…/orch)` title some builds give it.
_ORCH_TMUX_SERVER = re.compile(r"(?:^|\s)(?:-L\s*orch|-S\s*\S*/orch)(?=\s|$)|\(\S*/orch\)")


def harness_of(args: str) -> str | None:
    """The agent harness a process command line belongs to, or None. Looks at argv[0] and, for an interpreter, at
    the script or module it runs; never at later arguments (`vim claude.md` is not Claude Code). orch's own tmux
    server is one too (Mission Control's terminals)."""
    words = args.split()
    if not words:
        return None
    if _base(words[0]).rstrip(":") == "tmux" and _ORCH_TMUX_SERVER.search(args):
        return "orch-terminals"
    for marker, name in _PACKAGE_MARKERS:
        if any(marker in w for w in words[:3]):
            return name
    first = _base(words[0].rstrip(":"))
    if first in _HARNESS_NAMES:
        return _HARNESS_NAMES[first]
    if _INTERPRETER.match(first):
        rest = iter(words[1:])
        for w in rest:
            if w in ("-c", "-e", "--eval"):
                return None
            if w == "-m":
                return _HARNESS_NAMES.get(_base(next(rest, "")))
            if w.startswith("-"):
                continue
            return _HARNESS_NAMES.get(_base(w))
    return None


def _read_chain_proc(pid: int) -> list[tuple[int, str]] | None:
    out: list[tuple[int, str]] = []
    while pid >= 1 and len(out) < _MAX_DEPTH:  # PID 1 included: in a container the harness can be PID 1
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
        except OSError:
            break
        out.append((pid, args))
        try:
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            break
    return out


def _read_chain_ps(pid: int) -> list[tuple[int, str]] | None:
    try:
        res = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    table: dict[int, tuple[int, str]] = {}
    for line in res.stdout.splitlines():
        parts = line.split(None, 2)
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
        except (IndexError, ValueError):
            continue
    if pid not in table:
        return None
    out: list[tuple[int, str]] = []
    while pid >= 1 and pid in table and len(out) < _MAX_DEPTH:
        ppid, args = table[pid]
        out.append((pid, args))
        if ppid == pid:
            break
        pid = ppid
    return out


def _read_chain() -> list[tuple[int, str]] | None:
    """(pid, command line) of every ancestor, parent first; None when the tree cannot be read (or on Windows)."""
    if os.name == "nt":
        return None
    ppid = os.getppid()
    if os.path.isdir("/proc/self"):
        chain = _read_chain_proc(ppid)
        if chain:
            return chain
    return _read_chain_ps(ppid)


@functools.lru_cache(maxsize=1)
def _cached_chain():
    return _read_chain()


def process_chain() -> list[tuple[int, str]] | None:
    """This process's ancestors (read once per process: they do not change while it runs). Tests replace it."""
    return _cached_chain()


def ancestor_harness() -> str | None:
    """The harness among this process's ancestors; "unknown" when the tree is unreadable on POSIX (fail closed)."""
    chain = process_chain()
    if chain is None:
        return None if os.name == "nt" else "unknown"
    for _, args in chain:
        name = harness_of(args)
        if name:
            return name
    return None


def process_evidence() -> dict:
    """What a human event records about the process that wrote it: the harness found among its ancestors (None
    when there is none) and the ancestors' program names, parent first (None when the tree could not be read)."""
    chain = process_chain()
    names = None if chain is None else [_base(args.split()[0]) if args.split() else "?" for _, args in chain][:16]
    return {"harness": ancestor_harness(), "chain": names}


def agent_harness() -> str | None:
    if os.environ.get("ORCH_HARNESS"):
        return os.environ["ORCH_HARNESS"]
    for var, name in _ENV_MARKERS:
        if os.environ.get(var):
            return name
    if os.environ.get("AI_AGENT"):
        return re.sub(r"[^\w.-]+", "-", os.environ["AI_AGENT"])[:40] or "agent"
    return ancestor_harness()


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("ORCH_SESSION") or "local"


def is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def require_human_terminal(what: str, hint: str = _HINT) -> None:
    """Refuse `what` unless a human runs it from an interactive terminal outside any agent harness."""
    harness = agent_harness()
    if harness:
        raise HumanOnlyError(f"{what} refused: running inside an agent harness ({harness})", hint=hint)
    if not is_interactive():
        raise HumanOnlyError(f"{what} needs an interactive terminal", hint=hint)


def cli_actor() -> Actor:
    harness = agent_harness()
    if harness:
        return Actor("agent", harness, "cli", session_id())
    if is_interactive():
        return Actor("human", "you", "tty", session_id())
    return Actor("agent", "unknown", "cli", session_id())


def human_actor(ticket_id: str) -> Actor:
    require_human_terminal("human-only action")
    return confirm_typed(ticket_id)


def confirm_typed(ticket_id: str) -> Actor:
    """Ask the human to type the ticket id; the caller has already run require_human_terminal and its checks (#7)."""
    from orch.textsafe import visible
    typed = input(f"Type {visible(ticket_id)} to confirm: ").strip()
    if typed.upper() != ticket_id.upper():
        raise HumanOnlyError("confirmation did not match; nothing changed")
    return Actor("human", "you", "tty", None)
