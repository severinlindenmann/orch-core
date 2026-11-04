"""The data-dir format a hub test needs: what ``orch install hub`` leaves in the state dir (``format.json``)."""

from __future__ import annotations

import json
import os
from pathlib import Path

KIND = "hub"
FORMAT = 1


def write_format(state, kind: str = KIND, fmt: int = FORMAT, *, raw: bytes | None = None) -> Path:
    """Write ``<state>/format.json`` (0600), creating the dir (0700) if it is not there yet. ``raw`` is written as is."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "format.json"
    body = raw if raw is not None else (json.dumps({"orch": kind, "format": fmt}, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    return path
