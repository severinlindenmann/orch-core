"""Which files orch writes belong in git (#36). Durable files are shared records every clone needs: tickets, gates,
the counter, the event log, the phone-decision ledger and what addons keep under `.state/addons/<name>/records/`.
Local files are caches, locks, spools and per-machine state that orch can rebuild or that belong to one machine;
the managed block in `orchestrator/.gitignore` keeps them out of git. orch never commits anything itself: commits
belong to the human's (or the permitted agent's) own workflow, so `orch doctor` and `orch check` only name what
is left uncommitted."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from orch.core.fsutil import atomic_write_text

BEGIN = "# >>> orch: managed by `orch instructions sync` and `orch doctor --fix`; add your own lines outside this block"
END = "# <<< orch"

# (pattern in orchestrator/.gitignore, the same as a regex on the path relative to orchestrator/)
_LOCAL: tuple[tuple[str, str], ...] = (
    ("/temporary/", r"temporary/.*"),
    ("/.state/locks/", r"\.state/locks/.*"),
    ("/.state/index.json", r"\.state/index\.json"),
    ("/.state/addon-errors.log", r"\.state/addon-errors\.log"),
    ("/.state/guard-errors.log", r"\.state/guard-errors\.log"),
    ("/.state/needs-count", r"\.state/needs-count"),
    ("/.state/run/", r"\.state/run/.*"),
    ("/.state/**/*.lock", r"\.state/(.*/)?[^/]*\.lock"),
    ("**/.*.tmp", r"(.*/)?\.[^/]*\.tmp"),
)
# Addon folders are caches (snapshots, cursors, inbox/outbox spools) except their records/ folder.
_ADDON_LINES = ("/.state/addons/*", "!/.state/addons/*/", "/.state/addons/*/*", "!/.state/addons/*/records/")
_ADDON_LOCAL = r"\.state/addons/(?:[^/]+|[^/]+/(?!records/).*)"
_DURABLE = (r"config\.json", r"AGENTS\.orch\.md", r"\.gitignore", r"tickets/.*", r"artifacts/.*", r"static/.*",
            r"\.state/counter\.json", r"\.state/events\.jsonl", r"\.state/gates/.*", r"\.state/remote/ledger\.jsonl",
            r"\.state/addons/[^/]+/records/.*")
_LOCAL_RE = re.compile("|".join(f"(?:{rx})" for _, rx in _LOCAL) + f"|(?:{_ADDON_LOCAL})")
_DURABLE_RE = re.compile("|".join(f"(?:{rx})" for rx in _DURABLE))
# Files outside orchestrator/ that `orch instructions sync` writes: shared, so they belong in git too.
_SYNC_TARGETS = ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", ".github/copilot-instructions.md",
                 ".claude/skills", ".agents/skills")


def classify(rel: str) -> str | None:
    """"durable", "local" or None (orch does not know the file) for a path relative to orchestrator/."""
    rel = rel.replace("\\", "/")
    if _LOCAL_RE.fullmatch(rel):
        return "local"
    if _DURABLE_RE.fullmatch(rel):
        return "durable"
    return None


def ignore_block() -> str:
    lines = [BEGIN, "# Local only: caches, locks, spools and per-machine state. Tickets, gates, counter.json,",
             "# events.jsonl, remote/ledger.jsonl and addon records/ are shared records: commit them."]
    lines += [p for p, _ in _LOCAL] + list(_ADDON_LINES) + [END]
    return "\n".join(lines)


def apply_ignore_block(text: str | None) -> str:
    """The .gitignore text with the managed block replaced, or inserted first (lines after it can override it)."""
    block = ignore_block()
    if text is None or not text.strip():
        return block + "\n"
    crlf = "\r\n" in text
    lf = text.replace("\r\n", "\n")
    start, stop = lf.find(BEGIN), lf.find(END)
    if start != -1 and stop > start:
        out = lf[:start] + block + lf[stop + len(END):]
    else:
        out = block + "\n\n" + lf
    if not out.endswith("\n"):
        out += "\n"
    return out.replace("\n", "\r\n") if crlf else out


def _path(ws) -> Path:
    return ws.home / ".gitignore"


def _read(ws) -> str | None:
    try:
        return _path(ws).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def ignore_block_state(ws) -> str:
    """"ok", "missing" (no managed block yet) or "outdated" (a block from another orch version)."""
    text = _read(ws)
    if text is None or BEGIN not in text:
        return "missing"
    return "ok" if apply_ignore_block(text).replace("\r\n", "\n") == text.replace("\r\n", "\n") else "outdated"


def planned_ignore(ws) -> tuple[str, str]:
    """(new text, action) for orchestrator/.gitignore: action is "created", "updated" or "unchanged"."""
    old = _read(ws)
    new = apply_ignore_block(old)
    if old is None:
        return new, "created"
    return new, "unchanged" if old.replace("\r\n", "\n") == new.replace("\r\n", "\n") else "updated"


def write_ignore_block(ws) -> str:
    new, action = planned_ignore(ws)
    if action != "unchanged":
        atomic_write_text(_path(ws), new)
    return action


@dataclass
class GitView:
    root: Path
    uncommitted: list[str] = field(default_factory=list)  # durable records with changes git has not committed
    unclassified: list[str] = field(default_factory=list)  # untracked, not ignored, and not an orch record
    tracked_local: list[str] = field(default_factory=list)  # local files (caches, locks) that git tracks anyway


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def _rel_home(ws, top: Path, git_rel: str) -> str | None:
    try:
        return (top / git_rel).relative_to(ws.home.resolve()).as_posix()
    except ValueError:
        return None


def git_view(ws) -> GitView | None:
    """What git says about orch's files, or None when the workspace is not in a git repository (or git fails)."""
    root = ws.root.resolve()
    top_out = _git(root, "rev-parse", "--show-toplevel")
    if not top_out or not top_out.strip():
        return None
    top = Path(top_out.strip()).resolve()
    home = ws.home.resolve()
    if not home.is_relative_to(top):
        return None
    targets = [home] + [root / t for t in _SYNC_TARGETS if (root / t).exists() or t in ("AGENTS.md", "CLAUDE.md")]
    specs = [p.relative_to(top).as_posix() or "." for p in targets if p.is_relative_to(top)]
    status = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", *specs)
    tracked = _git(top, "ls-files", "-z", "--", home.relative_to(top).as_posix() or ".")
    if status is None or tracked is None:
        return None
    view = GitView(top)
    for entry in filter(None, status.split("\0")):
        code, rel = entry[:2], entry[3:]
        in_home = _rel_home(ws, top, rel)
        kind = "durable" if in_home is None else classify(in_home)
        if kind == "durable":
            view.uncommitted.append(_shown(top, root, rel))
        elif kind is None and code == "??":
            view.unclassified.append(_shown(top, root, rel))
    for rel in filter(None, tracked.split("\0")):
        in_home = _rel_home(ws, top, rel)
        if in_home is not None and classify(in_home) == "local":
            view.tracked_local.append(_shown(top, root, rel))
    return view


def few(paths: list[str], n: int = 5) -> str:
    shown = ", ".join(paths[:n])
    return shown + (f" and {len(paths) - n} more" if len(paths) > n else "")


def _shown(top: Path, root: Path, git_rel: str) -> str:
    """A path as the human sees it from the workspace root (the usual place to run git)."""
    try:
        return (top / git_rel).relative_to(root).as_posix()
    except ValueError:
        return git_rel


def changed_and_uncommitted(ws, paths: list[Path]) -> list[str]:
    """Those of `paths` (files orch just wrote) that git shows as changed or new, relative to the workspace root."""
    view = git_view(ws)
    if view is None:
        return []
    root = ws.root.resolve()
    wanted = set()
    for p in paths:
        try:
            wanted.add(p.resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return [p for p in view.uncommitted if p in wanted]
