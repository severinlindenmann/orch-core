"""The one process spawn the host may make (D-P2-9, D-P2-19): the git reader child.

``spawn_gitread`` runs ``[sys.executable, "-I", "-m", "orch.gitread"]`` with a fixed argv, an empty environment,
``close_fds``, pipes on stdin and stdout only, and a new process group. The audit hook of
:mod:`orch.host.audit` allows exactly this call.

The section 0.11 limits (CPU 10 s, address space 512 MiB, 64 open files, a 30 s alarm, no core, no file writes, no
new processes) are set by the child itself as its first action (:func:`orch.gitread.sandbox.apply_limits`), before it
reads its request: the argv is fixed and the child is orch's own code, and ``preexec_fn`` is unsafe in the threaded
host. The caller (:class:`orch.gitread.client.ReaderPool`) enforces the 30 s wall clock from outside as well and kills
the child's process group.
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
