"""``orch dashboard [args…]``: a thin launcher for the dashboard addon (``orch-dashboard``, ``orch_dashboard``).

The addon is a separate package with its own entry point; this subcommand only finds it and replaces the process with
it, passing every argument through (``orch dashboard open``, ``orch dashboard serve --help``). Nothing of core's state
is read or written here. Lookup order: the ``orch-dashboard`` console script next to this interpreter, the
``orch_dashboard`` module of this interpreter (``python -P -m orch_dashboard``), then the one on ``PATH`` (absolute
entries only). Not installed: a clear
error that names how to install it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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


_PROBE = "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('orch_dashboard') else 1)"


def _module_installed(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``python -P -m orch_dashboard`` can import the module: probed by the same interpreter, flag and
    environment the launch uses, so a package only in the current directory cannot satisfy discovery and then vanish."""
    try:
        res = subprocess.run(
            [sys.executable, "-P", "-c", _PROBE],
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _absolute_path(value: str | None) -> str:
    """``PATH`` without its empty and relative entries (they mean the current directory, which is a workspace an
    agent can write to: it must never be an exec source)."""
    return os.pathsep.join(p for p in (value or "").split(os.pathsep) if p and os.path.isabs(p))


def find_entry_point(env: Mapping[str, str], err: TextIO | None = None) -> list[str] | None:
    """The command that starts the dashboard addon, or ``None`` when it is not installed. Order: the console script
    beside this interpreter, the module of this interpreter (``python -P -m``: the current directory is not put on
    ``sys.path``), then ``PATH`` (absolute entries only; the resolved path is written to ``err``)."""
    beside = Path(sys.executable).parent / SCRIPT
    if beside.is_file() and os.access(beside, os.X_OK):
        return [str(beside)]
    if _module_installed(env):
        return [sys.executable, "-P", "-m", MODULE]
    found = shutil.which(SCRIPT, path=_absolute_path(env.get("PATH")))
    if found:
        if err is not None:
            err.write(f"orch dashboard: using {found} (found on PATH)\n")
        return [found]
    return None


def _exec(argv: list[str], env: Mapping[str, str]) -> None:  # a seam: tests replace it
    os.execve(argv[0], argv, dict(env))


def main(args: Sequence[str], *, env: Mapping[str, str], err: TextIO) -> int:
    command = find_entry_point(env, err)
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
