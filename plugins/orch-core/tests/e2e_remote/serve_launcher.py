"""Starts the dashboard command the way a person's terminal would, for the end-to-end test.

The command refuses to run unless a human is at an interactive terminal outside any agent harness; the repo's tests
replace the same two functions (orch.actor.process_chain, orch.actor.is_interactive). Everything else is the real
command: preflight, the relay tool, the transport child, the bridge loop.

E2E_TMUX_SOCKET points the terminals addon at a private tmux server, never the person's own `-L orch` one.
"""
import os
import sys

import orch.actor as actor

actor.process_chain = lambda: []
actor.is_interactive = lambda: True

sock = os.environ.get("E2E_TMUX_SOCKET")
if sock:
    from orch.dashboard import terminals
    terminals.SOCKET = sock

from orch.cli import run  # noqa: E402

sys.exit(run(["serve", *sys.argv[1:]]))
