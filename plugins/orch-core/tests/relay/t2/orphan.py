"""The parent-death proof shared by the harness self-test (a stand-in child) and the T2 test (the real relay)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import harness

ROOT = Path(harness.__file__).resolve().parents[3]  # plugins/orch-core: `tests` is importable from here
CHILD = """
import signal, sys, time
from pathlib import Path
from tests.relay.t2 import harness
signal.alarm(90)
mode, where = sys.argv[1], Path(sys.argv[2])
if mode == "fake":
    port = harness.free_port()
    p = harness.spawn(harness.fake_cmd(port), cwd=where, env={"PATH": "/usr/bin:/bin"}, log=where / "fake.log")
    print(p.pid, port, flush=True)
else:
    rp = harness.start_relay(Path(sys.argv[3]), where, None)
    print(rp.pgid, rp.port, flush=True)
signal.alarm(int(sys.argv[-1]))  # the default action ends this process: no atexit, no finally, no teardown
while True:
    time.sleep(1)
"""


def orphan_test(args: list[str], tmp_path: Path) -> None:
    """Start the harness in a child python, let an alarm kill that child (as a SIGKILL/OOM/alarm of pytest would) and
    prove the group it started ends by itself (the supervisor), the port closing too. Only our own PIDs are touched:
    the child (its own session) and, if the proof fails, the group it printed, after checking it is that group."""
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, *args], cwd=ROOT, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )  # fmt: skip
    try:
        assert child.stdout is not None
        line = child.stdout.readline().decode()
        pgid, port = (int(x) for x in line.split())
        assert harness.group_alive(pgid)
        child.wait(30)
        assert child.returncode == -signal.SIGALRM  # it died without any cleanup of its own
        end = time.monotonic() + 15
        while harness.group_alive(pgid) and time.monotonic() < end:
            time.sleep(0.1)
        alive = harness.group_alive(pgid)
        if alive:  # clean up only what is verifiably ours, then fail
            members = harness.group_members(pgid)
            if members and all(f"--port {port}" in cmd for _, _, cmd in members):
                os.killpg(pgid, signal.SIGKILL)
        assert not alive, f"group {pgid} outlived its parent"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            assert s.connect_ex((harness.HOST, port)) != 0
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(10)
