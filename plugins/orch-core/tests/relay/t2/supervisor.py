"""The T2 relay supervisor: ``python supervisor.py <command...>`` (stdlib only, run as its own process-group leader).

It starts ``<command>`` in its own (inherited) process group and ties the group's life to the harness's: its stdin is
a pipe whose write end only the harness holds, so when the harness's process ends in ANY way (``SIGKILL``, an alarm,
an OOM kill), the pipe reads EOF and the supervisor ends its whole group: ``SIGTERM``, ``SIGINT`` (uvicorn's forced
exit) one second later, ``SIGKILL`` three seconds after that, itself included. ``SIGTERM``/``SIGINT`` sent to the
group by the harness's normal teardown are ignored here (a handler, so the child still gets the default action after
``exec``), so the supervisor outlives the relay and exits with the child's status.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

TERM_TO_INT_S = 1.0
INT_TO_KILL_S = 3.0


def _ignore(signum: int, frame: object) -> None:
    pass


def _watch(pg: int, ending: threading.Event) -> None:
    try:
        while sys.stdin.buffer.read(1):
            pass
    except OSError:
        pass
    ending.set()
    for sig, pause in ((signal.SIGTERM, TERM_TO_INT_S), (signal.SIGINT, INT_TO_KILL_S), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(pg, sig)
        except ProcessLookupError:
            return
        time.sleep(pause)


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    signal.signal(signal.SIGTERM, _ignore)
    signal.signal(signal.SIGINT, _ignore)
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    ending = threading.Event()
    watcher = threading.Thread(target=_watch, args=(os.getpgrp(), ending), daemon=True)
    watcher.start()
    rc = child.wait()
    if ending.is_set():  # the harness is gone: finish ending the group (uv exits before the relay it ran)
        watcher.join()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
