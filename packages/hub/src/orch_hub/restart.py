"""Self-restart of ``orch-hub serve`` (M4, C3): after an address change the hub starts again as the same process.

``os.execve`` keeps the pid, so a service manager (launchd, systemd, a pidfile runner) sees no exit. Three rules (Opus
condition 5): the new process reads the advertised addresses and the API origin from ``hub-settings.json`` only, so the
old ``--web-origin`` / ``--api-origin`` arguments are dropped from the command line it is started with; every listener
and database is closed first (``serve`` has returned); and no descriptor above stderr survives the exec.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

#: start arguments that only the settings file decides after install
_FILE_ONLY = ("--web-origin", "--api-origin")


def restart_argv(orig: Sequence[str]) -> list[str]:
    """``orig`` (``sys.orig_argv``: interpreter, flags, script or ``-m``, arguments) without the origin arguments
    (``--x v`` and ``--x=v`` forms). Listen sockets and everything else stay."""
    out: list[str] = []
    skip = False
    for a in orig:
        if skip:
            skip = False
            continue
        if a in _FILE_ONLY:
            skip = True
            continue
        if a.startswith(tuple(f"{o}=" for o in _FILE_ONLY)):
            continue
        out.append(a)
    return out


def close_descriptors(first: int = 3) -> None:
    """Close every open descriptor from ``first`` up (stdin, stdout and stderr stay)."""
    try:
        top = os.sysconf("SC_OPEN_MAX")
    except ValueError, OSError:
        top = 4096
    os.closerange(first, min(top, 1 << 20) if top > 0 else 4096)


#: Variables that make an interpreter or the loader pull in code before the first line runs. orch-hub does not depend
#: on orch-core, so it cannot use ``orch.childenv``; this is the same rule, kept short: every ``PYTHON*`` (an empty
#: ``PYTHONPATH`` entry means the current directory, which ``-P`` does NOT stop), ``DYLD_*`` and ``LD_*``.
_LOADER_PREFIXES = ("PYTHON", "DYLD_", "LD_")


def restart_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without the loader variables."""
    return {k: v for k, v in env.items() if not k.startswith(_LOADER_PREFIXES)}


def exec_self(argv: Sequence[str] | None = None) -> None:
    """Replace this process by the same command line (minus the origin arguments). Does not return."""
    args = restart_argv(argv if argv is not None else sys.orig_argv)
    if not args:
        raise RuntimeError("no command line to restart")
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except OSError, ValueError:
            pass
    close_descriptors()
    # -P keeps the script directory and the current directory off sys.path; the cleaned environment keeps an empty
    # PYTHONPATH entry (which also means the current directory) and the loader variables from reaching the new process
    os.execve(sys.executable, [sys.executable, "-P", *args[1:]], restart_env(os.environ))
