"""Reading a v1 workspace, read-only.

A v1 workspace is the folder ``orchestrator/`` with ``config.json`` (``"schema": 1``),
``tickets/<status>/<KEY>[-slug].md`` (YAML frontmatter and ``## `` sections), ``artifacts/<KEY>/<name>`` and
the event log ``.state/events.jsonl``.

Everything here only opens files for reading; nothing is created, locked or repaired (v1's own ``Workspace.open``
creates folders, which is exactly what an import must not do to a workspace it does not own). v1 data is **untrusted**:
an agent could write any of these files, so a symlink, an oversized or odd file is refused, and what is read is data,
never instructions. The approval ledger (``~/.config/orch``) is never read.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import yamlsub

__all__ = ["V1Problem", "V1Ticket", "V1Workspace", "find_v1", "read_v1"]

STATUS_DIRS = ("backlog", "open", "in-progress", "waiting", "testing", "done")
MAX_TICKET_BYTES = 1 << 20
MAX_ARTIFACT_BYTES = 64 << 20
MAX_EVENT_LINE = 1 << 20
MAX_EVENTS_BYTES = 512 << 20
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]*-[0-9]+")
_FILE = re.compile(r"([A-Za-z][A-Za-z0-9]*-[0-9]+)(?:-[^/\\]*)?\.md")
_SECTION_NAMES = (
    "Ask",
    "Summary",
    "Context",
    "Requirements",
    "Acceptance criteria",
    "Out of scope",
    "Plan",
    "Tasks",
    "Current state",
    "Verification",
    "Log",
    "Findings",
)


@dataclass(frozen=True)
class V1Problem:
    """Something in the v1 workspace that is not imported (a file, a ticket), and why."""

    where: str
    why: str


@dataclass
class V1Ticket:
    key: str
    path: Path
    status_dir: str
    raw: bytes
    meta: dict[str, Any]
    sections: dict[str, str]
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class V1Workspace:
    home: Path  # the ``orchestrator`` folder
    config: dict[str, Any]
    tickets: list[V1Ticket]
    problems: list[V1Problem]

    @property
    def prefix(self) -> str:
        return str(self.config.get("id", {}).get("prefix", ""))

    @property
    def customer(self) -> str:
        return str(self.config.get("customer", ""))


def _dir_fd(home: Path, parts: tuple[str, ...]) -> int:
    """An fd of the directory ``home/parts``, opened one component at a time with ``O_NOFOLLOW`` relative to its
    parent: a symlink in any component (not just the last) fails closed. ``home`` itself is what the person named."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("this platform cannot refuse symlinks safely")
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part or "\\" in part or "\0" in part:
                raise OSError("unsafe path component")
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_rel(home: Path, parts: tuple[str, ...]) -> int:
    """An fd of the regular file ``home/parts`` (every component without a symlink, a regular file, not a device)."""
    dfd = _dir_fd(home, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    finally:
        os.close(dfd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("not a regular file")
    return fd


def _read_rel(home: Path, parts: tuple[str, ...], limit: int) -> bytes:
    fd = _open_rel(home, parts)
    try:
        if os.fstat(fd).st_size > limit:
            raise OSError(f"larger than {limit} bytes")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError(f"larger than {limit} bytes")
    return data


def _listdir_rel(home: Path, parts: tuple[str, ...]) -> list[str]:
    fd = _dir_fd(home, parts)
    try:
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)


def _is_home(p: Path) -> bool:
    try:
        doc = json.loads(_read_rel(p, ("config.json",), MAX_TICKET_BYTES))
        os.close(_dir_fd(p, ("tickets",)))
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(doc, dict) and doc.get("schema") == 1 and isinstance(doc.get("id"), dict)


def find_v1(path: str | os.PathLike[str]) -> Path | None:
    """The ``orchestrator`` folder of the v1 workspace at ``path`` (the project folder or the folder itself)."""
    p = Path(path)
    for cand in (p, p / "orchestrator"):
        if _is_home(cand):
            return cand
    return None


def read_artifact(home: Path, key: str, name: str) -> bytes:
    """The bytes of ``artifacts/<key>/<name>``: no ``..``, no symlink in any component, a regular file of at most
    64 MiB. (A hard link to a file outside cannot be told from a file; the person is told in the review.)"""
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or "\\" in p for p in parts) or "/" in key:
        raise OSError("unsafe artifact name")
    return _read_rel(home, ("artifacts", key, *parts), MAX_ARTIFACT_BYTES)


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_H2 = re.compile(r"^## (.+)$")


def _fence_step(fence: str | None, line: str) -> tuple[str | None, bool]:
    """``(fence open after the line, whether the line is a fence line)`` by v1's rule (CommonMark 4.5)."""
    m = _FENCE.match(line)
    if fence is None:
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            return m.group(1), True
        return None, False
    if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
        return None, True
    return fence, False


def split_sections(text: str) -> tuple[dict[str, str], str]:
    """``(sections, preamble)`` of a ticket body: a ``## `` line outside a fenced code block starts a section."""
    sections: dict[str, list[str]] = {}
    pre: list[str] = []
    current: list[str] | None = None
    fence: str | None = None
    h1_seen = False
    for line in text.split("\n"):
        fence, is_fence = _fence_step(fence, line)
        if not is_fence and fence is None:
            h2 = _H2.match(line)
            if h2:
                title = h2.group(1).rstrip().rstrip("#").rstrip().strip() or "#"
                current = sections.setdefault(title, [])
                continue
            if current is None and not h1_seen and re.match(r"^# (?!#)", line):
                h1_seen = True  # the generated title line
                continue
        (current if current is not None else pre).append(line)
    parsed = {name: "\n".join(lines).strip("\n").rstrip() for name, lines in sections.items()}
    return parsed, "\n".join(pre).strip()


def parse_ticket_file(raw: bytes) -> tuple[dict[str, Any], dict[str, str], str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("not UTF-8") from e
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise ValueError("no frontmatter")
    end = re.compile(r"^---[ \t]*$", re.M).search(text, 4)
    if end is None:
        raise ValueError("unterminated frontmatter")
    try:
        meta = yamlsub.loads(text[4 : end.start()]) or {}
    except yamlsub.YamlError as e:
        raise ValueError(f"frontmatter: {e}") from e
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        raise ValueError("frontmatter has no id")
    sections, pre = split_sections(text[end.end() :])
    return meta, sections, pre


def _events(home: Path, problems: list[V1Problem]) -> Iterator[dict[str, Any]]:
    try:
        fd = _open_rel(home, (".state", "events.jsonl"))
    except FileNotFoundError:
        return
    except OSError as e:
        problems.append(V1Problem(".state/events.jsonl", f"not read: {e.strerror or e}"))
        return
    bad = 0
    total = 0
    with os.fdopen(fd, "rb") as f:
        while True:
            line = f.readline(MAX_EVENT_LINE + 1)
            if not line:
                break
            total += len(line)
            if total > MAX_EVENTS_BYTES:
                problems.append(V1Problem(".state/events.jsonl", "larger than 512 MiB: the rest is not read"))
                break
            if len(line) > MAX_EVENT_LINE:
                bad += 1
                while line and not line.endswith(b"\n"):
                    line = f.readline(MAX_EVENT_LINE + 1)
                continue
            if not line.strip():
                continue
            try:
                d = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                bad += 1
                continue
            if isinstance(d, dict) and isinstance(d.get("kind"), str):
                yield d
            else:
                bad += 1
    if bad:
        problems.append(V1Problem(".state/events.jsonl", f"{bad} lines are not events and were left out"))


def read_v1(home: Path) -> V1Workspace:
    """Read every ticket of the v1 workspace ``home`` (the ``orchestrator`` folder), with its history. A ticket that
    cannot be read safely is a :class:`V1Problem`, not an error: the import reports it and goes on."""
    cfg = json.loads(_read_rel(home, ("config.json",), MAX_TICKET_BYTES))
    problems: list[V1Problem] = []
    tickets: dict[str, V1Ticket] = {}
    for status in STATUS_DIRS:
        try:
            names = _listdir_rel(home, ("tickets", status))
        except FileNotFoundError:
            continue
        except OSError as e:
            problems.append(V1Problem(f"tickets/{status}", f"not read: {e.strerror or e}"))
            continue
        for name in names:
            m = _FILE.fullmatch(name)
            if m is None:
                if not name.startswith("."):
                    problems.append(V1Problem(f"tickets/{status}/{name}", "not a ticket file name"))
                continue
            where = f"tickets/{status}/{name}"
            try:
                raw = _read_rel(home, ("tickets", status, name), MAX_TICKET_BYTES)
                meta, sections, _pre = parse_ticket_file(raw)
            except (OSError, ValueError) as e:
                problems.append(V1Problem(where, f"cannot be read: {e}"))
                continue
            key = m.group(1)
            if meta["id"] != key:
                problems.append(V1Problem(where, f"the file name says {key}, the frontmatter says {meta['id']}"))
                continue
            if key in tickets:
                problems.append(V1Problem(where, f"{key} is in two folders; the first one is imported"))
                continue
            tickets[key] = V1Ticket(key, home / where, status, raw, meta, sections)
    for e in _events(home, problems):
        t = tickets.get(e.get("ticket")) if isinstance(e.get("ticket"), str) else None
        if t is not None:
            t.events.append(e)
    return V1Workspace(
        home, cfg, sorted(tickets.values(), key=lambda t: (int(t.key.rsplit("-", 1)[1]), t.key)), problems
    )
