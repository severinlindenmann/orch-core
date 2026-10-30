"""The update journal ``<state>/install/update.json``: where an ``orch update`` is.

Phases, in order: ``stopped`` -> ``backed-up`` -> ``installed`` -> ``migrated:<root>`` (once per root) -> ``committed``.
Before ``committed`` everything can be rolled back; after it only forward (spec, "Data across updates"). Any phase
but ``committed`` is an unfinished update: :func:`~orch.migrate.format.open_root` refuses until ``orch update``
finishes or rolls it back. A journal that cannot be read counts as unfinished (fail closed).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch.ops.errors import OrchError
from orch.ops.runtime import state_dir

from .steps import atomic_write

__all__ = ["PHASES", "clear_journal", "journal_path", "read_journal", "unfinished", "write_journal"]

PHASES = ("stopped", "backed-up", "installed", "committed")
_MIGRATED = re.compile(r"migrated:[^\x00-\x1f]{1,4096}")
_LIMIT = 1 << 20


def journal_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(os.environ if env is None else env) / "install" / "update.json"


def _valid_phase(phase: object) -> bool:
    return isinstance(phase, str) and (phase in PHASES or _MIGRATED.fullmatch(phase) is not None)


def read_journal(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The journal, or None if there is none. An unreadable or malformed one raises ``update.interrupted``."""
    path = journal_path(env)
    try:
        with open(path, "rb") as f:
            raw = f.read(_LIMIT + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _interrupted(f"the update journal cannot be read ({exc.strerror or exc})") from None
    try:
        doc = json.loads(raw) if len(raw) <= _LIMIT else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or doc.get("v") != 1 or not _valid_phase(doc.get("phase")):
        raise _interrupted("the update journal is damaged")
    return doc


def _interrupted(why: str) -> OrchError:
    return OrchError(
        "update.interrupted", f"an update was interrupted ({why}): run `orch update` to finish or roll back"
    )


def _rank(phase: str) -> int:
    return 3 if phase.startswith("migrated:") else PHASES.index(phase) if phase in PHASES[:3] else 4


def write_journal(phase: str, env: Mapping[str, str] | None = None, **fields: Any) -> dict[str, Any]:
    """Record ``phase`` (other fields, e.g. ``from_version``, ``to_version``, ``backups``, are kept and updated)."""
    if not _valid_phase(phase):
        raise ValueError(f"unknown update phase {phase!r}")
    try:
        doc = read_journal(env) or {}
    except OrchError:
        if phase != "stopped":  # only a new update replaces a damaged journal
            raise
        doc = {}
    old = doc.get("phase")
    if old is not None and old != "committed" and _rank(phase) < _rank(old):
        raise ValueError(f"update phase {phase!r} cannot follow {old!r}")
    if old == "committed" and phase != "stopped" and phase != "committed":
        raise ValueError(f"update phase {phase!r} cannot follow 'committed'")
    doc.update(fields)
    doc.update({"v": 1, "phase": phase, "at": int(time.time())})
    path = journal_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(path, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())
    return doc


def unfinished(env: Mapping[str, str] | None = None) -> bool:
    try:
        doc = read_journal(env)
    except OrchError:
        return True
    return doc is not None and (doc["phase"] != "committed" or bool(doc.get("restoring")))


def clear_journal(env: Mapping[str, str] | None = None) -> None:
    try:
        journal_path(env).unlink()
    except FileNotFoundError:
        pass
