"""Data across updates: one format number per data root, ordered migration steps, a lock, a backup and an update
journal (spec: "Data across updates", orch-v2.md).

Nothing here runs at open time except :func:`open_root`, which only checks. Data is changed only by
``orch migrate`` / ``orch update`` (:func:`orch.migrate.runner.apply`).
"""

from __future__ import annotations

from .format import CURRENT, RootHandle, init_root, open_root, read_format, write_format
from .runner import apply, plan
from .steps import DEFAULT, Registry, Step, StepContext

__all__ = [
    "CURRENT",
    "DEFAULT",
    "Registry",
    "RootHandle",
    "Step",
    "StepContext",
    "apply",
    "init_root",
    "open_root",
    "plan",
    "read_format",
    "write_format",
]
