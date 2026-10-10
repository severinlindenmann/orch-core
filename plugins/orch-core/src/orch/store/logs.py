"""Reading the event logs (ticket-format §5, §5.5 "Reading a log"): strict parse of every line, schema check, and what
the store remembers per log (byte offset, last seq, the head of every event).

A line that is not exactly ``cj`` of its strict parse, or that fails its schema, ends the readable part of the log
(``error`` says where): the chain is broken there and nothing after it counts. ``host_sig``, ``prev`` and
authorization are the model's replay.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch import canon, schema

__all__ = ["LogInfo", "read_new_lines"]

WORKSPACE = "workspace"


@dataclass
class LogInfo:
    name: str  # "workspace" or the ticket uid
    path: Path
    offset: int = 0  # bytes of complete, good lines
    seq: int = 0
    heads: list[str] = field(default_factory=list)  # heads[i] is the head of seq i + 1
    error: str | None = None  # first unreadable line, if any
    error_seq: int = 0

    @property
    def head(self) -> str | None:
        return self.heads[-1] if self.heads else None

    @property
    def kind(self) -> str:
        return "workspace" if self.name == WORKSPACE else "ticket"


def read_new_lines(info: LogInfo, *, validate: bool = True) -> list[dict[str, Any]]:
    """Parse the lines of ``info.path`` after ``info.offset``; extend ``info`` with them; return the events.

    Stops at the first line that is not a good event, setting ``info.error`` (a torn final line without LF included).
    """
    try:
        with open(info.path, "rb") as f:
            f.seek(info.offset)
            data = f.read()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    pos = 0
    while pos < len(data) and info.error is None:
        nl = data.find(b"\n", pos)
        seq = info.seq + 1
        if nl < 0:
            info.error, info.error_seq = "the last line has no LF (a torn append)", seq
            break
        line = data[pos : nl + 1]
        try:
            e = canon.parse_event_line(line)
            if validate:
                schema.validate("event", e, log=info.kind)
            if e.get("seq") != seq:
                raise ValueError(f"seq is {e.get('seq')!r}, expected {seq}")
            head = canon.event_head(e)
        except (canon.HashError, schema.SchemaError, ValueError, KeyError, TypeError) as err:
            info.error, info.error_seq = f"{type(err).__name__}: {err}"[:300], seq
            break
        out.append(e)
        info.heads.append(head)
        info.seq = seq
        pos = nl + 1
        info.offset += len(line)
    return out


def file_size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except FileNotFoundError:
        return 0
