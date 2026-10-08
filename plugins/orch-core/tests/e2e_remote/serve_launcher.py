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

fresh_limit = os.environ.get("E2E_FRESH_LIMIT")
if fresh_limit:  # D9 allows 6 fresh confirmations per 10 minutes per device; the run asks for more than that
    from orch.remote.bridge_host import budgets
    budgets.FRESH_LIMIT = int(fresh_limit)

lease_ms = os.environ.get("E2E_LEASE_MS")
if lease_ms:  # the typing lease is 15 minutes; the run shortens it so that its end can be watched
    from orch.remote.bridge_host import host_check
    host_check.LEASE_MS = int(lease_ms)

from orch.cli import run  # noqa: E402

sys.exit(run(["serve", *sys.argv[1:]]))
