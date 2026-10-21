"""A CLI call releases every fd it opened when it returns (CI incident 21 Oct: a test worker reached fd 1024).

The store's root fd, its cached directory fds, the index and pin databases and the session records' pinned root are
closed when ``main`` returns, not when the cyclic GC gets to them: the host and a test worker run thousands of calls
in one process."""

from __future__ import annotations

import gc
import os


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_calls_do_not_hold_fds(ws, cli):
    cli("new", "A ticket")
    gc.disable()  # what the cyclic GC would free is exactly what must not be needed
    try:
        cli("status"), cli("show"), cli("list")  # warm up (imports, caches)
        before = _open_fds()
        for _ in range(20):
            assert cli("status").code == 0
            cli("show")
            cli("list")
        after = _open_fds()
    finally:
        gc.enable()
    assert after - before <= 2, f"{after - before} fds held after 60 calls"
