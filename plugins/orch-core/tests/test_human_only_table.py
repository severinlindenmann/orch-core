"""The guard's one table of human-only subcommands (guard.HUMAN_SUBCOMMANDS, with guard._HUMAN_VERBS): every spelling
check derives from it, and it is derived from the CLI itself: every command that asks the human's terminal is in it."""
import inspect
import itertools
import re

import pytest
import typer

from orch.cli import app
from orch.hooks import guard
from orch.hooks.guard import evaluate


def _commands():
    def walk(c, path):
        if hasattr(c, "commands"):
            for name, sub in c.commands.items():
                yield from walk(sub, path + (name,))
        else:
            yield path, c
    return list(walk(typer.main.get_command(app), ()))


def _covered(path) -> bool:
    """The command's words match a table entry, or start one (the entry's other words are its arguments:
    `factory dark` with `on`)."""
    if path[0] in guard._HUMAN_VERBS:
        return True
    return any(all(w == "*" or path[i] in w.split("|") for i, w in enumerate(p[:len(path)]))
               for p in guard.HUMAN_SUBCOMMANDS)


# CLI commands that ask the human's terminal for a reason other than signing a decision, each with why it may stay
# open to agents' text (the command refuses an agent itself).
NOT_IN_TABLE = {
    ("addon", "install"): "addon administration: _ADDON_ADMIN, its own rule",
    ("addon", "update"): "addon administration: _ADDON_ADMIN, its own rule",
    ("addon", "trust"): "addon administration: _ADDON_ADMIN, its own rule",
    ("addon", "enable"): "addon administration: _ADDON_ADMIN, its own rule",
    ("addon", "disable"): "addon administration: _ADDON_ADMIN, its own rule",
    ("addon", "remove"): "addon administration: _ADDON_ADMIN, its own rule",
    ("addon", "rollback"): "addon administration: _ADDON_ADMIN, its own rule",
    ("serve",): "the dashboard: _SERVE, its own rule",
}


def test_every_cli_command_that_asks_the_human_is_in_the_table():
    asks = [path for path, c in _commands()
            if c.callback and re.search(r"require_human_terminal|confirm_typed", inspect.getsource(c.callback))]
    assert asks, "the walk found no human-only command"
    missing = [p for p in asks if not _covered(p) and p not in NOT_IN_TABLE]
    assert missing == [], f"human-only CLI commands the guard's table does not name: {missing}"


def _concrete(path):
    words = [w.split("|") if w != "*" else ["set"] for w in path]
    return [list(x) for x in itertools.product(*words)]


CASES = [w for p in guard.HUMAN_SUBCOMMANDS for w in _concrete(p)] + [[v, "L-1"] for v in guard._HUMAN_VERBS]


@pytest.mark.parametrize("words", CASES, ids=lambda w: " ".join(w))
def test_every_table_entry_is_refused_in_every_spelling(ws, words):
    text = " ".join(words)
    argv = ", ".join(f'"{w}"' for w in words)
    for cmd in (f"orch {text}", f"uv run orch --json {text}", f"o''rch {text}", f"sh -c 'orch {text}'",
                f"python3 -c 'from orch.cli import app; app([{argv}])'",
                f"python3 -c 'from orch.cli import app; app()' {text}"):
        d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)})
        assert not d.allow, cmd
