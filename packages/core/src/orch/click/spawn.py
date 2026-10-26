"""Start the click signer beside the dashboard server (orch-v2-host §0.8.6 item 5)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Mapping

from .client import ClickClient

__all__ = ["start"]


def start(env: Mapping[str, str] | None = None) -> ClickClient:
    """A socketpair (both ends close-on-exec), the child ``[sys.executable, -I, -m, orch.click, --fd, n]`` with only
    its end passed, then that end closed here. ``env`` is the child's environment (default: this process's)."""
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        n = child.fileno()
        proc = subprocess.Popen(
            [sys.executable, "-I", "-m", "orch.click", "--fd", str(n)],
            pass_fds=(n,),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            env=dict(os.environ if env is None else env),
        )
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    return ClickClient(parent, proc)
