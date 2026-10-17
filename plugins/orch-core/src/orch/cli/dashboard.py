"""``orch dashboard [args…]``: a thin launcher for the dashboard addon (``orch-dashboard``, ``orch_dashboard``).

The addon is a separate package with its own entry point; this subcommand only finds it and replaces the process with
it, passing every argument through (``orch dashboard open``, ``orch dashboard serve --help``). Nothing of core's state
is read or written here. Lookup order: the ``orch-dashboard`` console script next to this interpreter, the
``orch_dashboard`` module of this interpreter (``python -P -m orch_dashboard``), then the one on ``PATH`` (absolute
entries only). Not installed: a clear
error that names how to install it.

Under an agent's environment (``ORCH_GRANT`` or ``ORCH_SESSION`` set and non-empty) it refuses with ``human_only``
(exit 3), the shape of ``orch sign``'s refusal, before anything is looked up or started: a dashboard session acts as the
person (#542). Only ``stop`` passes (it mints nothing and only ends the person's server). The addon refuses again in its
own entry point, so going around ``orch dashboard`` does not help. A speed bump in tier A, not a boundary: same UID.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

__all__ = ["AGENT_ENV", "INSTALL_HINT", "agent_refusal", "find_entry_point", "main"]

SCRIPT = "orch-dashboard"
MODULE = "orch_dashboard"
INSTALL_HINT = (
    "the dashboard addon is not installed: install it into the same uv tool environment as orch: "
    'uv tool install --force --with "orch-dashboard @ git+https://github.com/severinlindenmann/orch-core@develop'
    '#subdirectory=addons/dashboard/server" '
    '"git+https://github.com/severinlindenmann/orch-core@develop#subdirectory=plugins/orch-core"'
)
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
REFUSAL = (
    "the dashboard is a person's; an agent never opens it (unset ORCH_GRANT and ORCH_SESSION in your own terminal)"
)
STOP_ONLY = (["stop"], ["--stop"])


def agent_refusal(args: Sequence[str], env: Mapping[str, str]) -> str | None:
    """The ``human_only`` message when an agent's environment asks for anything but ``stop``, else ``None``."""
    if any(env.get(name) for name in AGENT_ENV) and list(args) not in STOP_ONLY:
        return REFUSAL
    return None


_PROBE = "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"


def _module_installed(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``python -P -m orch_dashboard`` can import the module: probed by the same interpreter, flag and
    environment the launch uses, so a package only in the current directory cannot satisfy discovery and then vanish."""
    try:
        res = subprocess.run(
            [sys.executable, "-P", "-c", _PROBE, MODULE],
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


def _launch_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` with ``PYTHONPATH`` reduced to its absolute entries: Python turns an empty entry into the current
    directory at startup, which ``-P`` does not prevent (the probe and the exec use this same environment)."""
    out = dict(env)
    if "PYTHONPATH" in out:
        kept = _absolute_path(out["PYTHONPATH"])
        if kept:
            out["PYTHONPATH"] = kept
        else:
            del out["PYTHONPATH"]
    return out


def main(args: Sequence[str], *, env: Mapping[str, str], err: TextIO) -> int:
    refused = agent_refusal(args, env)
    if refused is not None:
        err.write(f"orch dashboard: human_only: {refused}\n")
        return 3
    env = _launch_env(env)
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
