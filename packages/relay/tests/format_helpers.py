"""The data-dir format a relay test needs: what ``orch install relay`` leaves in the data dir."""

import json
import os
from pathlib import Path

KIND = "relay"
FORMAT = 1


def write_format(data, kind=KIND, fmt=FORMAT, *, raw=None):
    """Write ``<data>/format.json`` (0600), creating the dir (0700) if it is not there yet.
    ``raw`` is written as is."""
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = data / "format.json"
    body = raw if raw is not None else json.dumps({"orch": kind, "format": fmt}).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    return path


def ensure_format(data, kind=KIND, fmt=FORMAT):
    """``write_format`` unless the dir already has a ``format.json`` (a test that wrote its own)."""
    if not (Path(data) / "format.json").exists():
        write_format(data, kind, fmt)
