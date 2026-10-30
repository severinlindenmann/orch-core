"""``plan`` and ``apply``: the only code that migrates a root.

``apply`` takes the EXCLUSIVE lock of ``<root>/format.lock`` without waiting: if any service holds the shared lock
(it has the root open) the answer is "orch is running". Steps run in order; ``format.json`` is written after EACH
step, so a crash between two steps resumes at the next one, and a second ``apply`` is a no-op.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orch.ops.errors import OrchError

from .format import lock_root, read_format, write_format
from .steps import DEFAULT, Registry, Step, StepContext

__all__ = ["Plan", "apply", "plan"]


@dataclass(frozen=True)
class Plan:
    kind: str
    path: Path
    at: int  # the format the data is at
    target: int  # the format the code is at
    steps: tuple[Step, ...]

    @property
    def pending(self) -> bool:
        return bool(self.steps)


def _read(root: Path, kind: str, reg: Registry) -> tuple[int, int]:
    if kind not in reg.current:
        raise ValueError(f"unknown root kind {kind!r}")
    at = read_format(root, kind)
    if at is None:
        raise OrchError("format.missing", f"{root} is not orch data (no format.json)")
    target = reg.current[kind]
    if at > target:
        raise OrchError("format.newer", f"this data was made by a newer orch (format {at}): update orch")
    return at, target


def plan(root: Path, kind: str, reg: Registry = DEFAULT) -> Plan:
    """What ``apply`` would do. Reads only; takes no lock."""
    root = Path(root)
    at, target = _read(root, kind, reg)
    return Plan(kind, root, at, target, tuple(reg.pending(kind, at)))


def apply(
    root: Path,
    kind: str,
    reg: Registry = DEFAULT,
    *,
    after_step: Callable[[Step], None] | None = None,
) -> list[str]:
    """Run the pending steps of ``root``; returns the names applied (empty when there was nothing to do).
    ``after_step`` is called after each step's ``format.json`` is written (a seam for crash tests and progress)."""
    root = Path(root)
    fd = lock_root(root, exclusive=True)
    try:
        done: list[str] = []
        while True:
            p = plan(root, kind, reg)  # re-read after every step: format.json is the truth
            if not p.steps:
                return done
            step = p.steps[0]
            step.apply(StepContext(root))
            if step.verify is not None:
                step.verify(root)
            write_format(root, kind, step.to_n)
            done.append(step.name)
            if after_step is not None:
                after_step(step)
    finally:
        os.close(fd)
