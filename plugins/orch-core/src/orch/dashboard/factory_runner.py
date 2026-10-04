"""The AI Factory runner inside the dashboard server (docs/factory.md, phase 4): a background round every few seconds
that calls orch.core.factory_runner.tick with orch's own tmux server (the Terminals' `tmux -L orch`). It does nothing
unless the factory is on, tmux exists, and the human started an epic from the dashboard."""
from __future__ import annotations

import asyncio
import logging

from orch.dashboard import launch, terminals
from orch.dashboard.views import HUMAN
from orch.errors import OrchError, UsageError

log = logging.getLogger("orch.factory")
ROUND_SECONDS = 15


class TmuxLauncher:
    def alive(self) -> set[str]:
        r = terminals.tmux(["list-sessions", "-F", "#{session_name}"])
        return set(r.stdout.split()) if r.returncode == 0 else set()

    def start(self, name: str, cwd: str, argv: list[str]) -> int:
        # a session inside the workspace, so Mission Control's Terminals page lists it
        cmd = launch.argv_for("tmux", cwd=cwd, command_argv=argv, name=name, script_path=None, custom=[])
        proc = launch.subprocess.Popen(cmd, start_new_session=True, stdout=launch.subprocess.DEVNULL,
                                       stderr=launch.subprocess.DEVNULL, stdin=launch.subprocess.DEVNULL)
        if proc.wait(timeout=10) != 0:
            raise UsageError(f"tmux could not start a session named {name}")
        r = terminals.tmux(["display-message", "-p", "-t", f"={name}:", "#{pane_pid}"])
        return int(r.stdout.strip())  # ValueError (no pid) ends the session in the caller: nothing runs unbound

    def stop(self, name: str) -> None:
        terminals.end(name)


def run_once(ws, launcher=None) -> list[str]:
    from orch.core import factory_runner, factory_sessions, permits
    if not terminals.available() or not (permits.enabled(ws) or factory_sessions.bindings(ws)):
        return []
    return factory_runner.tick(ws, HUMAN, launcher or TmuxLauncher(), settings=launch.load_settings())


async def loop(ws, seconds: float = ROUND_SECONDS) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            for line in await asyncio.to_thread(run_once, ws):
                log.info("factory runner: %s", line)
        except (OrchError, OSError) as e:  # e.g. refused under an agent harness: say so, keep serving
            log.warning("factory runner: %s", e)
        except Exception:  # a round must never take the dashboard down
            log.exception("factory runner round failed")
