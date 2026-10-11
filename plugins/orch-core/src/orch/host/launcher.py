"""The one process spawn the host may make (D-P2-9, D-P2-19): the git reader child.

``spawn_gitread`` runs ``[sys.executable, "-I", "-m", "orch.gitread"]`` with a fixed argv, an empty environment,
``close_fds``, pipes on stdin and stdout only, and a new process group. The audit hook of
:mod:`orch.host.audit` allows exactly this call. The ``setrlimit`` limits of section 0.11 and the reader itself are
H3b1; until then the child fails to start ``orch.gitread`` and exits.
"""

from __future__ import annotations

import subprocess
import sys

__all__ = ["gitread_argv", "spawn_gitread"]


def gitread_argv() -> list[str]:
    return [sys.executable, "-I", "-m", "orch.gitread"]


def spawn_gitread() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        gitread_argv(),
        env={},
        close_fds=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
