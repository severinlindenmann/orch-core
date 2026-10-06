"""Schedules inside the dashboard server (docs/schedules.md): the switch (the `schedules` default addon), the
runner's background round and its tmux server.

Runs use a tmux server of their own, on a socket beside the factory's in the guarded permits folder of the orch
config dir, so they are neither Terminals' sessions nor the factory's. Nothing runs unless the human enabled the
addon in this workspace, tmux is installed and a schedule is armed.
"""
from __future__ import annotations

import asyncio
import logging

from orch.core import schedule_runner
from orch.dashboard import factory_runner
from orch.dashboard.reach import LOCAL_HUMAN
from orch.errors import OrchError

log = logging.getLogger("orch.schedules")
ADDON = "schedules"
ROUND_SECONDS = 20


def addon_on(ws) -> bool:
    """The `schedules` default addon is enabled (and trusted) here: it is the switch for all of this."""
    try:
        return ws.addons.get(ADDON) is not None
    except Exception:  # a broken addon registry never breaks a page or the runner
        return False


class Launcher(factory_runner.TmuxLauncher):
    @property
    def socket(self):
        return factory_runner.socket_path().parent / "schedules"


def status_path(ws):
    """Where the addon's Today tile reads the runner's status (the addon's own state folder)."""
    return ws.state_dir / "addons" / ADDON / "status.json"


def run_once(ws, launcher=None) -> list[str]:
    from orch.core import schedules
    on = addon_on(ws)
    if not factory_runner.available() or not (on and schedules.definitions(ws) or schedules.open_runs(ws)):
        return []
    lines = schedule_runner.tick(ws, LOCAL_HUMAN, launcher or Launcher(), enabled=on)
    if on:
        schedule_runner.write_status(ws, status_path(ws))
    return lines


def shutdown(ws, launcher=None) -> list[str]:
    from orch.core import schedules
    if not schedules.open_runs(ws) or not factory_runner.available():
        return []
    return schedule_runner.stop_all(ws, LOCAL_HUMAN, launcher or Launcher())


async def loop(ws, seconds: float = ROUND_SECONDS) -> None:
    try:
        await asyncio.sleep(1)  # let the first page come up first
        while True:
            try:
                for line in await asyncio.to_thread(run_once, ws):
                    log.info("schedules: %s", line)
            except (OrchError, OSError) as e:  # e.g. refused under an agent harness: say so, keep serving
                log.warning("schedules: %s", e)
            except Exception:  # a round must never take the dashboard down
                log.exception("schedules round failed")
            await asyncio.sleep(seconds)
    finally:
        try:
            for line in await asyncio.shield(asyncio.to_thread(shutdown, ws)):
                log.info("schedules: %s", line)
        except BaseException:  # never block the dashboard's own shutdown
            log.exception("schedules shutdown failed")
