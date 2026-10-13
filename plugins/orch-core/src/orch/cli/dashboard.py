"""``orch dashboard [args…]``: a thin launcher for the dashboard addon (``orch-dashboard``, ``orch_dashboard``).

The addon is a separate package with its own entry point; this subcommand only finds it and replaces the process with
it, passing every argument through (``orch dashboard open``, ``orch dashboard serve --help``). Nothing of core's state
is read or written here. Lookup order: the ``orch-dashboard`` console script next to this interpreter, the one on
``PATH``, then the ``orch_dashboard`` module of this interpreter (``python -m orch_dashboard``). Not installed: a clear
error that names how to install it.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

__all__ = ["INSTALL_HINT", "find_entry_point", "main"]

SCRIPT = "orch-dashboard"
MODULE = "orch_dashboard"
INSTALL_HINT = (
    "the dashboard addon is not installed: install it into the environment that runs orch "
    "(pip install orch-dashboard, or uv tool install orch-dashboard, or from a checkout: "
    "pip install -e addons/dashboard/server)"
)


def find_entry_point(env: Mapping[str, str]) -> list[str] | None:
    """The command that starts the dashboard addon, or ``None`` when it is not installed."""
    beside = Path(sys.executable).parent / SCRIPT
    if beside.is_file() and os.access(beside, os.X_OK):
        return [str(beside)]
    found = shutil.which(SCRIPT, path=env.get("PATH"))
    if found:
        return [found]
    try:
        if importlib.util.find_spec(MODULE) is not None:
            return [sys.executable, "-m", MODULE]
    except (ImportError, ValueError):
        pass
    return None


def _exec(argv: list[str], env: Mapping[str, str]) -> None:  # a seam: tests replace it
    os.execve(argv[0], argv, dict(env))


def main(args: Sequence[str], *, env: Mapping[str, str], err: TextIO) -> int:
    command = find_entry_point(env)
    if command is None:
        err.write(f"orch dashboard: {INSTALL_HINT}\n")
        return 3
    argv = [*command, *args]
    try:
        _exec(argv, env)
    except OSError as exc:
        err.write(f"orch dashboard: could not start {command[0]}: {exc}\n")
        return 9
    return 0  # only reached when a test's _exec returns
