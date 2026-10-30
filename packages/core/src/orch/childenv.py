"""The one place that decides which environment a child process of orch gets.

Anything orch starts for the person runs as the person, often next to their keys. Variables that make a program load
code from somewhere the person did not choose (an interpreter's search path, the dynamic loader's preload list, a
shell start-up file) therefore never reach a child. Every spawn site uses :func:`sanitize` (or :func:`minimal`)."""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = ["minimal", "sanitize"]

#: Variables that load code or libraries into a child before it runs a line of its own.
LOADER_NAMES = frozenset(
    {
        "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE", "PYTHONBREAKPOINT",
        "PYTHONEXECUTABLE", "PYTHONWARNINGS", "PYTHONPLATLIBDIR",
        "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT", "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "BASH_ENV", "ENV", "CDPATH", "GCONV_PATH", "LOCPATH", "NLSPATH",
    }
)  # fmt: skip
LOADER_PREFIXES = ("DYLD_", "LD_")  # macOS dyld and the Linux loader (LD_PRELOAD, LD_LIBRARY_PATH, LD_AUDIT, ...)

#: What a child that needs nothing from the environment (a signing helper) keeps.
_MINIMAL_KEEP = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL")
_MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _absolute(value: str) -> str:
    return os.pathsep.join(p for p in value.split(os.pathsep) if p and os.path.isabs(p))


def sanitize(env: Mapping[str, str], *, absolute_pythonpath: bool = False) -> dict[str, str]:
    """``env`` without the loader variables. ``absolute_pythonpath`` keeps the absolute entries of ``PYTHONPATH``
    (an addon started from a source checkout needs them); an empty or relative entry means the current directory,
    which is never kept."""
    out: dict[str, str] = {}
    for k, v in env.items():
        if k in LOADER_NAMES or k.startswith(LOADER_PREFIXES):
            continue
        out[k] = v
    if absolute_pythonpath and env.get("PYTHONPATH"):
        kept = _absolute(env["PYTHONPATH"])
        if kept:
            out["PYTHONPATH"] = kept
    return out


def minimal(env: Mapping[str, str]) -> dict[str, str]:
    """Only what a plain helper binary needs: a fixed ``PATH`` and the person's basic identity and locale."""
    out = {k: env[k] for k in _MINIMAL_KEEP if k in env}
    out["PATH"] = _MINIMAL_PATH
    return out
