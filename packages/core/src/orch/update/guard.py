"""Services hold the host state root while they run (spec, "Data across updates", S3).

:func:`hold_state_root` is called by the host, the agent runner and the dashboard server at start. It takes the shared
lock of ``<state>/format.lock`` for the life of the process (so ``orch migrate``/``orch update`` see "orch is running"),
and refuses while an update journal is unfinished, unless this is the quarantined start of ``orch update`` itself.
A state directory with no ``format.json`` yet is new: it is born at the code's format.
"""

from __future__ import annotations

from pathlib import Path

from orch.migrate.format import CURRENT, RootHandle, open_root, read_format, write_format

__all__ = ["hold_state_root"]


def hold_state_root(state: Path, *, quarantine_ok: bool = False) -> RootHandle:
    state = Path(state)
    env = {"ORCH_STATE_DIR": str(state)}
    if state.is_dir() and read_format(state, "host") is None:
        from orch.migrate.journal import unfinished

        if not unfinished(env):
            write_format(state, "host", CURRENT["host"])
    return open_root(state, "host", env, quarantine_ok=quarantine_ok)
